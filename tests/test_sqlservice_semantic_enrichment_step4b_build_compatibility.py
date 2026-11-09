"""
Focused test — SQL Architecture Correction, Phase 6, Step 4B, Part 5:
current-build compatibility.

Confirms, end to end against a real (temporary) SQLite store, the exact
claim Part 5 requires: adding core.sqlservice.retrieval_documents' Phase 6
Step 1A "column_descriptors" metadata field to an otherwise-unchanged
document changes the retrieval BUILD's own document-set identity (a new
build is created, never reused) while leaving that document's own
document_hash unchanged — so its cached embedding (keyed ONLY by
document_hash) is fully reusable by the new build without a single new
embedding-provider call. Also confirms core.sqlservice.
semantic_enrichment_service's own legacy-fallback (Step 4A) actually
engages for the OLD, descriptor-less build and stops being needed once
column_descriptors is present.

Uses a temporary on-disk SQLite database only — never the real
data/toolsmith.db, never a real embedding/semantic-enrichment provider.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_step4b_build_compatibility.py -v
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4b-compat-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-step4b-compat-salt-long-enough-12345678901")

import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.embedding_store import STATUS_OK as EMBED_STATUS_OK
from core.sqlservice.embedding_store import get_embedding_build_completeness, insert_embeddings
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import (
    OUTCOME_CREATED,
    load_stored_retrieval_documents,
    persist_retrieval_build,
)
from core.sqlservice.semantic_enrichment_service import build_object_semantic_inputs


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
    import shutil
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(embedding_store_module, "get_connection", lambda: _make_connection(path))
    return path


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


_TEXT = "OBJECT: dbo.Legacy\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  LegacyID INTEGER [PK]"


def _doc(*, with_descriptors: bool):
    metadata = {
        "column_count": 1,
        "column_names": ("LegacyID",),
        "primary_key_columns": ("LegacyID",),
        "confirmed_relationship_edges": (),
        "business_name": None, "description": None, "synonyms": (),
    }
    if with_descriptors:
        metadata["column_descriptors"] = (
            {"column_name": "LegacyID", "normalized_sql_type": "INTEGER", "is_primary_key": True, "is_foreign_key": False},
        )
    else:
        metadata["column_descriptors"] = ()
    return RetrievalDocument(
        source_id=1, object_fqn="dbo.Legacy", object_type="TABLE",
        schema_name="dbo", object_name="Legacy", retrieval_tier=1,
        document_text=_TEXT, document_hash=hashlib.sha256(_TEXT.encode()).hexdigest(),
        metadata=MappingProxyType(metadata),
    )


def _fingerprint(seed):
    return {
        "fingerprint_version": 1,
        "fingerprint_hash": hashlib.sha256(f"fp-{seed}".encode()).hexdigest(),
        "table_set_hash": hashlib.sha256(f"ts-{seed}".encode()).hexdigest(),
    }


def _batch(docs):
    return RetrievalDocumentBatch(
        source_id=1, snapshot_id=1, snapshot_version=1, status=STATUS_READY,
        documents=tuple(docs), counts=MappingProxyType({}), warnings=(),
    )


def test_adding_column_descriptors_creates_new_build_but_reuses_document_hash_and_embeddings(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)

    old_doc = _doc(with_descriptors=False)
    new_doc = _doc(with_descriptors=True)
    assert old_doc.document_hash == new_doc.document_hash  # document_text never includes column_descriptors

    old_result = persist_retrieval_build(_batch([old_doc]), _fingerprint("v1"), "alice")
    assert old_result.outcome == OUTCOME_CREATED

    new_result = persist_retrieval_build(_batch([new_doc]), _fingerprint("v1"), "alice")
    assert new_result.outcome == OUTCOME_CREATED  # a genuinely NEW build -- document-set identity changed
    assert new_result.build.id != old_result.build.id
    assert new_result.build.document_set_hash != old_result.build.document_set_hash

    old_stored = load_stored_retrieval_documents(old_result.build.id, 1, "alice")[0]
    new_stored = load_stored_retrieval_documents(new_result.build.id, 1, "alice")[0]
    assert old_stored.document_hash == new_stored.document_hash  # unchanged, as predicted

    # Embed under the OLD build; the NEW build must report zero missing --
    # full embedding reuse, zero new provider calls, keyed by document_hash
    # alone (never build_id).
    vector = (0.1, 0.2, 0.3, 0.4)
    insert_result = insert_embeddings(
        old_result.build.id, 1, "alice", "fake", "fake-model", len(vector),
        {old_stored.document_hash: vector},
    )
    assert insert_result.status == EMBED_STATUS_OK

    completeness_for_new_build = get_embedding_build_completeness(
        new_result.build.id, 1, "alice", "fake", "fake-model", len(vector),
    )
    assert completeness_for_new_build.status == EMBED_STATUS_OK
    assert completeness_for_new_build.missing_document_hashes == ()
    assert completeness_for_new_build.total_documents == 1


def test_legacy_fallback_engages_without_descriptors_and_stops_once_present(db_path):
    old_doc = _doc(with_descriptors=False)
    new_doc = _doc(with_descriptors=True)

    (old_input,), _ = build_object_semantic_inputs([old_doc])
    (new_input,), _ = build_object_semantic_inputs([new_doc])

    assert old_input.columns[0].normalized_sql_type == "OTHER"  # never guessed
    assert new_input.columns[0].normalized_sql_type == "INTEGER"
    assert old_input.columns[0].is_primary_key is True  # legacy fallback still resolves PK via primary_key_columns
    assert new_input.columns[0].is_primary_key is True
