"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4B, Part 3:
semantic-enrichment preparation coordinator
(core/sqlservice/semantic_enrichment_preparation.py).

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db. Every provider call is a FakeSemanticEnrichmentProvider
defined in this file — never OpenAISemanticEnrichmentProvider against a
real network.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_preparation.py -v
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4b-preparation-secret-long-enough-1234567")
os.environ.setdefault("USER_ID_SALT", "test-step4b-preparation-salt-long-enough-12345678")

import data.models as models_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.semantic_enrichment_preparation as preparation_module
import core.sqlservice.semantic_enrichment_store as store_module
from core.sqlservice.join_planner import clear_join_planner_cache
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from core.sqlservice.schema_linker import clear_schema_linker_cache
from core.sqlservice.semantic_enrichment_preparation import (
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_PARTIAL,
    prepare_source_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_providers import (
    ProviderSemanticEnrichmentResponse,
    SemanticEnrichmentProvider,
)
from core.sqlservice.semantic_enrichment_store import get_enrichment_by_cache_key


# ── Fixture plumbing (mirrors tests/test_sqlservice_embedding_coordinator.py) ─

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
    now = "2026-09-13T00:00:00Z"
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
    now = "2026-09-13T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id, columns=(("StuID", "INTEGER", True, False),), business_name=None, tier=1):
    schema_name, object_name = object_fqn.split(".", 1)
    column_descriptors = tuple(
        {"column_name": name, "normalized_sql_type": sql_type, "is_primary_key": pk, "is_foreign_key": fk}
        for name, sql_type, pk, fk in columns
    )
    metadata = {
        "column_count": len(columns),
        "column_names": tuple(c[0] for c in columns),
        "column_descriptors": column_descriptors,
        "has_primary_key": any(c[2] for c in columns),
        "primary_key_columns": tuple(c[0] for c in columns if c[2]),
        "foreign_key_targets": (),
        "confirmed_relationship_targets": (),
        "inferred_relationship_targets": (),
        "confirmed_relationship_sources": (),
        "inferred_relationship_sources": (),
        "confirmed_relationship_edges": (),
        "status_columns": (), "date_columns": (), "measure_columns": (),
        "domain": None, "business_name": business_name, "description": None, "synonyms": (),
        "has_business_name": bool(business_name), "has_description": False, "synonym_count": 0,
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


# ── Fake provider ────────────────────────────────────────────────────────


class FakeSemanticEnrichmentProvider(SemanticEnrichmentProvider):
    def __init__(self, responses=None, *, provider_id="fake", model="fake-model-1"):
        self._provider_id = provider_id
        self._model = model
        self._queue = list(responses) if responses is not None else []
        self.calls: list[str] = []

    @property
    def provider_id(self):
        return self._provider_id

    @property
    def model(self):
        return self._model

    def enrich_batch(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append(user_prompt)
        assert self._queue, "FakeSemanticEnrichmentProvider called with no queued response left"
        raw = self._queue.pop(0)
        return ProviderSemanticEnrichmentResponse(raw=raw, model=self._model)


def _object_item(object_fqn, columns):
    # Phase 6 Step 4C1: a real PRIMARY_KEY is structurally clear and is
    # never a selected column any more (see core.sqlservice.
    # semantic_enrichment_selection) -- a response entry for one is now
    # rejected, exactly like an unknown column. This fixture mirrors real
    # v2 behavior: only non-PK columns ever get a "columns" entry.
    return {
        "object_fqn": object_fqn, "business_name": "Student", "description": "A student",
        "object_role": "ENTITY", "entity_concepts": ["student"], "aliases": ["learner"],
        "supported_operations": ["COUNT"], "confidence": 0.8,
        "columns": [
            {
                "column_name": name, "business_label": name, "semantic_role": "IDENTIFIER",
                "date_role": None, "measure_role": None, "dimension_role": None,
                "identifier_role": "PRIMARY_KEY" if pk else None, "confidence": 0.6,
            }
            for name, _type, pk, _fk in columns
            if not pk
        ],
    }


def _valid_response_for_docs(docs_and_columns):
    return {"objects": [_object_item(fqn, cols) for fqn, cols in docs_and_columns]}


# ── First run / repeat run / one-changed-object ────────────────────────────


def test_first_run_requests_missing_objects_and_builds_catalog(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, 1)
    doc_a = _doc("dbo.Stu", source_id=1)
    doc_b = _doc("dbo.Course", source_id=1, columns=(("CourseID", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b])

    provider = FakeSemanticEnrichmentProvider([
        _valid_response_for_docs([
            ("dbo.Stu", [("StuID", "INTEGER", True, False)]),
            ("dbo.Course", [("CourseID", "INTEGER", True, False)]),
        ]),
    ])
    result = prepare_source_semantic_enrichment(1, "alice", provider)

    assert result.status == STATUS_OK
    assert result.eligible_object_count == 2
    assert result.generated_count == 2
    assert result.reused_count == 0
    assert result.catalog_object_count == 2
    assert result.catalog_fingerprint is not None
    assert len(provider.calls) == 1  # both objects fit in one default-sized batch


def test_repeat_run_makes_zero_provider_calls(db_path):
    _insert_source(db_path, 2)
    _insert_snapshot(db_path, 2, 2)
    doc = _doc("dbo.Stu", source_id=2)
    _make_ready_build(db_path, [doc], source_id=2, snapshot_id=2)

    provider = FakeSemanticEnrichmentProvider([_valid_response_for_docs([("dbo.Stu", [("StuID", "INTEGER", True, False)])])])
    first = prepare_source_semantic_enrichment(2, "alice", provider)
    assert first.generated_count == 1
    assert len(provider.calls) == 1

    second = prepare_source_semantic_enrichment(2, "alice", provider)  # same provider -- no queued responses left
    assert second.status == STATUS_OK
    assert second.generated_count == 0
    assert second.reused_count == 1
    assert len(provider.calls) == 1  # unchanged -- zero NEW provider calls


def test_one_changed_object_only_re_enriched_others_reused(db_path):
    _insert_source(db_path, 3)
    _insert_snapshot(db_path, 3, 3)
    doc_a = _doc("dbo.Stu", source_id=3)
    doc_b = _doc("dbo.Course", source_id=3, columns=(("CourseID", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b], source_id=3, snapshot_id=3, seed="v1")

    provider = FakeSemanticEnrichmentProvider([
        _valid_response_for_docs([("dbo.Stu", [("StuID", "INTEGER", True, False)]), ("dbo.Course", [("CourseID", "INTEGER", True, False)])]),
    ])
    first = prepare_source_semantic_enrichment(3, "alice", provider)
    assert first.generated_count == 2
    stu_row_id_before = get_enrichment_by_cache_key(
        3, "alice", object_fqn="dbo.Stu", semantic_input_hash=_current_hash(db_path, 3, "alice", "dbo.Stu"),
        provider_id="fake", model="fake-model-1", contract_version=first.contract_version,
    ).id

    # Change dbo.Course's structure (new column) -- a NEW retrieval build.
    doc_b_v2 = _doc(
        "dbo.Course", source_id=3,
        columns=(("CourseID", "INTEGER", True, False), ("Title", "TEXT", False, False)),
    )
    _make_ready_build(db_path, [doc_a, doc_b_v2], source_id=3, snapshot_id=3, seed="v2")

    # "Title" is an ordinary, already-self-descriptive text column -- Phase
    # 6 Step 4C1's selection policy correctly asks for no column-level
    # enrichment for it either (same as CourseID's real PK); only the
    # object-level record is expected in the response.
    provider.calls.clear()
    provider._queue = [_valid_response_for_docs([("dbo.Course", [])])]
    second = prepare_source_semantic_enrichment(3, "alice", provider)

    assert second.generated_count == 1  # only dbo.Course
    assert second.reused_count == 1     # dbo.Stu reused, unaffected by dbo.Course's change
    assert len(provider.calls) == 1
    assert "dbo.Course" in provider.calls[0]
    assert "dbo.Stu" not in provider.calls[0]

    stu_row_id_after = get_enrichment_by_cache_key(
        3, "alice", object_fqn="dbo.Stu", semantic_input_hash=_current_hash(db_path, 3, "alice", "dbo.Stu"),
        provider_id="fake", model="fake-model-1", contract_version=second.contract_version,
    ).id
    assert stu_row_id_after == stu_row_id_before  # untouched, not merely re-derived identically


# ── Partial batch failure / safe resume ─────────────────────────────────────


def test_partial_batch_failure_reports_status_partial_and_persists_the_rest(db_path):
    _insert_source(db_path, 4)
    _insert_snapshot(db_path, 4, 4)
    doc_a = _doc("dbo.Stu", source_id=4)
    doc_b = _doc("dbo.Course", source_id=4, columns=(("CourseID", "INTEGER", True, False),))
    _make_ready_build(db_path, [doc_a, doc_b], source_id=4, snapshot_id=4)

    bad_item = _object_item("dbo.Stu", [("StuID", "INTEGER", True, False)])
    del bad_item["description"]  # malformed -- fails strict validation for its own batch
    good_response = _valid_response_for_docs([("dbo.Course", [("CourseID", "INTEGER", True, False)])])

    # max_objects_per_batch=1 forces two separate batches (deterministic
    # order is by (family_key, object_fqn); dbo.Course sorts before dbo.Stu).
    provider = FakeSemanticEnrichmentProvider([good_response, {"objects": [bad_item]}])
    result = prepare_source_semantic_enrichment(4, "alice", provider, max_objects_per_batch=1)

    assert result.status == STATUS_PARTIAL
    assert result.batch_validation_failed_count == 1
    assert result.generated_count == 1
    assert result.catalog_object_count == 1
    assert get_enrichment_by_cache_key(
        4, "alice", object_fqn="dbo.Course", semantic_input_hash=_current_hash(db_path, 4, "alice", "dbo.Course"),
        provider_id="fake", model="fake-model-1", contract_version=result.contract_version,
    ) is not None
    assert get_enrichment_by_cache_key(
        4, "alice", object_fqn="dbo.Stu", semantic_input_hash=_current_hash(db_path, 4, "alice", "dbo.Stu"),
        provider_id="fake", model="fake-model-1", contract_version=result.contract_version,
    ) is None  # the failed batch's object was never persisted


def _current_hash(db_path, source_id, user_id, object_fqn):
    from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build, load_stored_retrieval_documents
    from core.sqlservice.semantic_enrichment_service import build_object_semantic_inputs, compute_object_semantic_input_hash

    build = get_latest_ready_retrieval_build(source_id, user_id)
    documents = load_stored_retrieval_documents(build.id, source_id, user_id)
    inputs, _skipped = build_object_semantic_inputs(documents)
    obj = next(i for i in inputs if i.object_fqn == object_fqn)
    return compute_object_semantic_input_hash(obj)


def test_safe_resume_after_partial_failure_completes_on_retry(db_path):
    _insert_source(db_path, 5)
    _insert_snapshot(db_path, 5, 5)
    doc = _doc("dbo.Stu", source_id=5)
    _make_ready_build(db_path, [doc], source_id=5, snapshot_id=5)

    bad_item = _object_item("dbo.Stu", [("StuID", "INTEGER", True, False)])
    del bad_item["description"]
    provider = FakeSemanticEnrichmentProvider([{"objects": [bad_item]}])
    first = prepare_source_semantic_enrichment(5, "alice", provider)
    assert first.status == STATUS_PARTIAL
    assert first.generated_count == 0

    # Resume: the failed object was never persisted, so a retry with a valid
    # response for it completes cleanly.
    provider2 = FakeSemanticEnrichmentProvider([_valid_response_for_docs([("dbo.Stu", [("StuID", "INTEGER", True, False)])])])
    second = prepare_source_semantic_enrichment(5, "alice", provider2)
    assert second.status == STATUS_OK
    assert second.generated_count == 1
    assert second.catalog_object_count == 1


# ── Routine statuses / no READY build ───────────────────────────────────────


def test_no_ready_build_reports_not_prepared(db_path):
    _insert_source(db_path, 6)
    provider = FakeSemanticEnrichmentProvider([])
    result = prepare_source_semantic_enrichment(6, "alice", provider)
    assert result.status == STATUS_NOT_PREPARED
    assert provider.calls == []


# ── Typed-descriptor coverage / old-build fallback (Part 5) ────────────────


def test_typed_descriptor_coverage_and_legacy_fallback(db_path):
    _insert_source(db_path, 7)
    _insert_snapshot(db_path, 7, 7)
    typed_doc = _doc("dbo.Stu", source_id=7)
    legacy_doc = _doc("dbo.Legacy", source_id=7, columns=(("LegacyID", "INTEGER", True, False),))
    # Simulate a pre-Step-1A stored document: no column_descriptors at all,
    # only the always-present legacy fields.
    legacy_metadata = dict(legacy_doc.metadata)
    legacy_metadata["column_descriptors"] = ()
    from dataclasses import replace
    legacy_doc = replace(legacy_doc, metadata=MappingProxyType(legacy_metadata))

    _make_ready_build(db_path, [typed_doc, legacy_doc], source_id=7, snapshot_id=7)

    provider = FakeSemanticEnrichmentProvider([
        _valid_response_for_docs([
            ("dbo.Legacy", [("LegacyID", "INTEGER", True, False)]),
            ("dbo.Stu", [("StuID", "INTEGER", True, False)]),
        ]),
    ])
    result = prepare_source_semantic_enrichment(7, "alice", provider)

    assert result.objects_missing_typed_descriptors == 1
    assert result.status == STATUS_OK
    assert result.generated_count == 2  # the legacy-fallback object is still enrichable end to end


# ── Safe logging ────────────────────────────────────────────────────────────


def test_no_raw_provider_response_or_exception_text_in_logs(db_path, caplog):
    _insert_source(db_path, 8)
    _insert_snapshot(db_path, 8, 8)
    doc = _doc("dbo.Stu", source_id=8)
    _make_ready_build(db_path, [doc], source_id=8, snapshot_id=8)

    secret_marker = "TOTALLY_SECRET_PAYLOAD_MARKER_123"
    bad_item = _object_item("dbo.Stu", [("StuID", "INTEGER", True, False)])
    bad_item["description"] = secret_marker  # would only surface if raw payload were logged
    del bad_item["business_name"]  # force validation failure
    provider = FakeSemanticEnrichmentProvider([{"objects": [bad_item]}])

    with caplog.at_level(logging.DEBUG):
        prepare_source_semantic_enrichment(8, "alice", provider)

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert secret_marker not in combined
