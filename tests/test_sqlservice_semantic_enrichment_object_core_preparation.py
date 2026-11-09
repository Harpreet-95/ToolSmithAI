"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4C2:
OBJECT_CORE preparation coordinator
(core/sqlservice/semantic_enrichment_object_core_preparation.py).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db. Every provider call is a FakeSemanticEnrichmentProvider
defined in this file — never OpenAISemanticEnrichmentProvider against a
real network.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_preparation.py -v
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
os.environ.setdefault("JWT_SECRET", "test-step4c2-preparation-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-step4c2-preparation-salt-long-enough-1234")

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
    STATUS_NOT_PREPARED,
    STATUS_OBJECT_CORE_PARTIAL,
    STATUS_OBJECT_CORE_READY,
    STATUS_UNOWNED,
    prepare_source_object_core_enrichment,
)
from core.sqlservice.semantic_enrichment_phases import PHASE_COLUMN_DEFERRED, PHASE_OBJECT_CORE
from core.sqlservice.semantic_enrichment_providers import (
    ProviderSemanticEnrichmentResponse,
    SemanticEnrichmentProvider,
)
from core.sqlservice.semantic_enrichment_store import get_enrichment_by_cache_key, insert_semantic_enrichment
from core.sqlservice.semantic_enrichment_service import (
    EnrichedObjectSemantics,
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    build_object_semantic_inputs,
    compute_object_core_cache_key,
)


# ── Fixture plumbing (mirrors tests/test_sqlservice_semantic_enrichment_preparation.py) ─

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
    now = "2026-09-14T00:00:00Z"
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
    now = "2026-09-14T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id, columns=(("StuID", "INTEGER", True, False),), tier=1):
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
        raw = self._responder(user_prompt)
        return ProviderSemanticEnrichmentResponse(raw=raw, model=self._model)


def _auto_respond(user_prompt: str) -> dict:
    ids = re.findall(r"OBJECT (o\d+):", user_prompt)
    return {
        "objects": [
            {
                "id": i, "label": "Student", "role": "ENTITY", "concepts": ["student"],
                "aliases": ["learner"], "description": "A student record.", "confidence": 0.9,
            }
            for i in ids
        ]
    }


# ── First run / repeat run / readiness ──────────────────────────────────


def test_first_run_generates_all_and_reports_ready(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, 1)
    doc_a = _doc("dbo.Stu", source_id=1)
    doc_b = _doc("dbo.Course", source_id=1, columns=(("CourseID", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b])

    provider = FakeObjectCoreProvider()
    result = prepare_source_object_core_enrichment(1, "alice", lambda: provider)

    assert result.status == STATUS_OBJECT_CORE_READY
    assert result.eligible_object_count == 2
    assert result.generated_count == 2
    assert result.reused_count == 0
    assert result.missing_object_fqns == ()
    assert result.contract_version == SEMANTIC_ENRICHMENT_CONTRACT_VERSION


def test_repeat_run_makes_zero_provider_calls_and_stays_ready(db_path):
    _insert_source(db_path, 2)
    _insert_snapshot(db_path, 2, 2)
    doc = _doc("dbo.Stu", source_id=2)
    _make_ready_build(db_path, [doc], source_id=2, snapshot_id=2)

    provider = FakeObjectCoreProvider()
    first = prepare_source_object_core_enrichment(2, "alice", lambda: provider)
    assert first.status == STATUS_OBJECT_CORE_READY
    assert first.generated_count == 1

    provider2 = FakeObjectCoreProvider()
    second = prepare_source_object_core_enrichment(2, "alice", lambda: provider2)
    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert provider2.calls == []  # zero NEW provider calls


def test_no_ready_build_reports_not_prepared(db_path):
    _insert_source(db_path, 3)
    provider = FakeObjectCoreProvider()
    result = prepare_source_object_core_enrichment(3, "alice", lambda: provider)
    assert result.status == STATUS_NOT_PREPARED
    assert provider.calls == []


# ── Partial batch failure / safe resume ─────────────────────────────────


def test_partial_batch_failure_reports_partial_and_persists_the_rest(db_path):
    _insert_source(db_path, 4)
    _insert_snapshot(db_path, 4, 4)
    doc_a = _doc("dbo.Alpha", source_id=4)
    doc_b = _doc("dbo.Beta", source_id=4, columns=(("CourseID", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b], source_id=4, snapshot_id=4)

    def responder(user_prompt):
        if "dbo.Alpha" in user_prompt:
            return {"objects": []}  # malformed -- fails this batch alone
        return _auto_respond(user_prompt)

    provider = FakeObjectCoreProvider(responder)
    result = prepare_source_object_core_enrichment(4, "alice", lambda: provider, max_objects_per_batch=1)

    assert result.status == STATUS_OBJECT_CORE_PARTIAL
    assert result.batch_validation_failed_count == 1
    assert result.generated_count == 1
    assert result.missing_object_fqns == ("dbo.Alpha",)


def test_safe_resume_after_partial_failure_completes_and_becomes_ready(db_path):
    _insert_source(db_path, 5)
    _insert_snapshot(db_path, 5, 5)
    doc = _doc("dbo.Stu", source_id=5)
    _make_ready_build(db_path, [doc], source_id=5, snapshot_id=5)

    provider = FakeObjectCoreProvider(lambda user_prompt: {"objects": []})
    first = prepare_source_object_core_enrichment(5, "alice", lambda: provider)
    assert first.status == STATUS_OBJECT_CORE_PARTIAL
    assert first.generated_count == 0
    assert first.missing_object_fqns == ("dbo.Stu",)

    provider2 = FakeObjectCoreProvider()
    second = prepare_source_object_core_enrichment(5, "alice", lambda: provider2)
    assert second.status == STATUS_OBJECT_CORE_READY
    assert second.generated_count == 1
    assert second.missing_object_fqns == ()


# ── Bounded concurrency actually used ────────────────────────────────────


def test_concurrency_used_is_bounded_by_batch_count_and_max_concurrency(db_path):
    _insert_source(db_path, 6)
    _insert_snapshot(db_path, 6, 6)
    docs = [_doc(f"dbo.Obj{i}", source_id=6, columns=(("PK", "INTEGER", True, False),)) for i in range(10)]
    _make_ready_build(db_path, docs, source_id=6, snapshot_id=6)

    provider = FakeObjectCoreProvider()
    result = prepare_source_object_core_enrichment(
        6, "alice", lambda: provider, max_objects_per_batch=2, max_concurrency=3,
    )
    assert result.status == STATUS_OBJECT_CORE_READY
    assert result.batch_count == 5
    assert result.concurrency_used == 3


# ── Phase isolation: OBJECT_CORE and COLUMN_DEFERRED never collide ─────


def test_object_core_and_column_deferred_rows_coexist_without_collision(db_path):
    _insert_source(db_path, 7)
    _insert_snapshot(db_path, 7, 7)
    doc = _doc("dbo.Stu", source_id=7)
    _make_ready_build(db_path, [doc], source_id=7, snapshot_id=7)

    (obj_input,), _ = build_object_semantic_inputs([doc])

    # Manually plant a COLUMN_DEFERRED row at the SAME object/provider/model
    # (a different phase, so it must not satisfy OBJECT_CORE completeness).
    from core.sqlservice.semantic_enrichment_service import (
        EvidenceItem,
        PROVENANCE_MODEL_INFERRED_SEMANTIC,
        compute_cache_key,
        generate_semantic_enrichment,
    )
    from tests.test_sqlservice_semantic_enrichment_preparation import (
        FakeSemanticEnrichmentProvider,
        _valid_response_for_docs,
    )

    column_deferred_provider = FakeSemanticEnrichmentProvider(
        [_valid_response_for_docs([("dbo.Stu", [("StuID", "INTEGER", True, False)])])]
    )
    cd_result = generate_semantic_enrichment([doc], column_deferred_provider)
    cd_key = cd_result.cache_keys_by_object_fqn["dbo.Stu"]
    insert_semantic_enrichment(
        "alice", source_id=7, object_fqn="dbo.Stu", semantic_input_hash=cd_key.semantic_input_hash,
        provider_id="fake", model="fake-model-1", contract_version=cd_result.contract_version,
        object_semantics=cd_result.enriched_objects[0], column_semantics=cd_result.enriched_columns,
        phase=PHASE_COLUMN_DEFERRED,
    )

    # OBJECT_CORE must still report NOT_PREPARED-equivalent (nothing cached
    # for ITS phase) and run generation normally -- the COLUMN_DEFERRED row
    # is invisible to it.
    object_core_provider = FakeObjectCoreProvider()
    result = prepare_source_object_core_enrichment(7, "alice", lambda: object_core_provider)
    assert result.status == STATUS_OBJECT_CORE_READY
    assert result.generated_count == 1  # a fresh OBJECT_CORE call was still made
    assert len(object_core_provider.calls) == 1

    object_core_key = compute_object_core_cache_key(obj_input, "fake", "fake-model-1")
    stored_object_core = get_enrichment_by_cache_key(
        7, "alice", object_fqn="dbo.Stu", semantic_input_hash=object_core_key.semantic_input_hash,
        provider_id="fake", model="fake-model-1", contract_version=object_core_key.contract_version,
        phase=PHASE_OBJECT_CORE,
    )
    stored_column_deferred = get_enrichment_by_cache_key(
        7, "alice", object_fqn="dbo.Stu", semantic_input_hash=cd_key.semantic_input_hash,
        provider_id="fake", model="fake-model-1", contract_version=cd_result.contract_version,
        phase=PHASE_COLUMN_DEFERRED,
    )
    assert stored_object_core is not None
    assert stored_column_deferred is not None
    assert stored_object_core.id != stored_column_deferred.id
    assert stored_object_core.phase == PHASE_OBJECT_CORE
    assert stored_column_deferred.phase == PHASE_COLUMN_DEFERRED
    assert stored_object_core.columns == ()  # OBJECT_CORE never persists column-level semantics


# ── Safe logging ──────────────────────────────────────────────────────────


def test_no_raw_provider_response_or_exception_text_in_logs(db_path, caplog):
    _insert_source(db_path, 8)
    _insert_snapshot(db_path, 8, 8)
    doc = _doc("dbo.Stu", source_id=8)
    _make_ready_build(db_path, [doc], source_id=8, snapshot_id=8)

    secret_marker = "TOTALLY_SECRET_PAYLOAD_MARKER_456"

    def responder(user_prompt):
        return {"objects": [{
            "id": "o1", "label": "Student", "role": "ENTITY", "concepts": [], "aliases": [],
            "description": secret_marker,
        }]}  # missing "confidence" -- forces validation failure

    provider = FakeObjectCoreProvider(responder)
    with caplog.at_level(logging.DEBUG):
        prepare_source_object_core_enrichment(8, "alice", lambda: provider)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_marker not in combined
