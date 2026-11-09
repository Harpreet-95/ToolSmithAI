"""
Focused unit tests — SQL rebuild Phase 2, Step 1: provider-independent
embedding cache (core/sqlservice/embedding_store.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server, never OpenAI. Schema is created via
the real `data.models.init_db()` (monkeypatched to a temp file) so these
tests exercise the actual production schema, not a hand-maintained copy.
No embeddings are ever generated here — every vector in this file is a
small hand-written list of floats standing in for a real provider's
output.

Run:
    venv/Scripts/pytest tests/test_sqlservice_embedding_store.py -v
"""

import math
import os
import shutil
import sqlite3
import struct

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import core.sqlservice.embedding_store as embedding_store_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.embedding_store import (
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_OK,
    STATUS_UNOWNED,
    deserialize_vector,
    find_missing_document_hashes,
    get_embedding_build_completeness,
    get_embeddings_for_build,
    insert_embeddings,
    serialize_vector,
)
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import persist_retrieval_build
from types import MappingProxyType
import hashlib
import json


# ── Fixture plumbing (mirrors tests/test_sqlservice_retrieval_store.py) ──

MIGRATION_012_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "012_create_sql_retrieval_store.sql",
)
MIGRATION_013_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "013_create_embedding_cache.sql",
)


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
    """Full real schema via the actual data.models.init_db() — see the
    identical fixture in test_sqlservice_retrieval_store.py for why this is
    a module-scoped template copied per-test rather than reinitialized."""
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


def _insert_source(db_path, source_id, user_id="alice"):
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
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
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', '{}', ?, ?)",
        (snapshot_id, source_id, version, now, now),
    )
    conn.commit()
    conn.close()


def _doc(object_fqn, *, source_id=1, object_type="TABLE", tier=1, text=None, doc_hash=None, metadata=None):
    if text is None:
        text = f"OBJECT: {object_fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    if doc_hash is None:
        doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    schema_name, object_name = object_fqn.split(".", 1)
    return RetrievalDocument(
        source_id=source_id, object_fqn=object_fqn, object_type=object_type,
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash,
        metadata=MappingProxyType(metadata if metadata is not None else {"column_count": 1}),
    )


def _batch(source_id, snapshot_id, snapshot_version, documents, status=STATUS_READY):
    return RetrievalDocumentBatch(
        source_id=source_id, snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        status=status, documents=tuple(documents), counts=MappingProxyType({}), warnings=(),
    )


def _fingerprint(*, version=1, fingerprint_hash=None, table_set_hash=None):
    if fingerprint_hash is None:
        fingerprint_hash = hashlib.sha256(b"fingerprint-a").hexdigest()
    if table_set_hash is None:
        table_set_hash = hashlib.sha256(b"table-set-a").hexdigest()
    return {
        "fingerprint_version": version,
        "fingerprint_hash": fingerprint_hash,
        "table_set_hash": table_set_hash,
    }


def _make_ready_build(db_path, source_id=1, snapshot_id=1, object_fqns=("dbo.learners", "dbo.tracks"), user_id="alice"):
    """End-to-end: real source + snapshot + a READY sql_retrieval_builds row
    with real sql_retrieval_documents rows, via the actual Step 5 persist
    path — never a hand-inserted row standing in for the real schema."""
    _insert_source(db_path, source_id, user_id=user_id)
    _insert_snapshot(db_path, snapshot_id, source_id=source_id)
    docs = [_doc(fqn, source_id=source_id) for fqn in object_fqns]
    batch = _batch(source_id, snapshot_id, 1, docs)
    result = persist_retrieval_build(batch, _fingerprint(), user_id)
    assert result.build is not None
    return result.build


PROVIDER = "openai"
MODEL = "text-embedding-3-small"
DIMS = 4


def _vec(seed):
    return [float(seed), float(seed) + 0.5, -float(seed), 0.0]


# ── 1. Float32 round-trip ─────────────────────────────────────────────────

def test_serialize_deserialize_round_trips_float32_precision():
    values = [0.1, -2.5, 3.0, 1234.5]
    blob = serialize_vector(values)
    restored = deserialize_vector(blob)
    assert len(restored) == len(values)
    for original, back in zip(values, restored):
        assert math.isclose(original, back, rel_tol=1e-6, abs_tol=1e-6)


def test_serialized_vector_is_compact_binary_not_json():
    blob = serialize_vector([1.0, 2.0, 3.0, 4.0])
    assert isinstance(blob, (bytes, bytearray))
    assert len(blob) == 4 * 4  # 4 dims * float32
    with pytest.raises(Exception):
        json.loads(blob.decode("utf-8"))


# ── 2. Deterministic encoding ──────────────────────────────────────────────

def test_encoding_is_deterministic_little_endian():
    values = [1.5, -2.25, 0.0, 100.75]
    blob = serialize_vector(values)
    expected = struct.pack("<4f", *values)
    assert blob == expected
    assert blob == serialize_vector(values)  # repeatable


# ── 3. Invalid / non-finite vector rejection ───────────────────────────────

def test_empty_vector_rejected():
    with pytest.raises(ValueError):
        serialize_vector([])


def test_nan_rejected():
    with pytest.raises(ValueError):
        serialize_vector([1.0, float("nan"), 3.0])


def test_infinite_rejected():
    with pytest.raises(ValueError):
        serialize_vector([1.0, float("inf"), 3.0])
    with pytest.raises(ValueError):
        serialize_vector([1.0, float("-inf"), 3.0])


def test_non_numeric_element_rejected():
    with pytest.raises(ValueError):
        serialize_vector([1.0, "not-a-number", 3.0])


def test_deserialize_rejects_truncated_blob():
    with pytest.raises(ValueError):
        deserialize_vector(b"\x00\x00\x00")  # 3 bytes, not a multiple of 4


def test_deserialize_rejects_empty_blob():
    with pytest.raises(ValueError):
        deserialize_vector(b"")


# ── 4. Dimension validation ─────────────────────────────────────────────────

def test_insert_rejects_dimension_mismatch(db_path):
    build = _make_ready_build(db_path)
    doc_hash = _doc("dbo.learners").document_hash
    with pytest.raises(ValueError):
        insert_embeddings(
            build.id, 1, "alice", PROVIDER, MODEL, DIMS,
            {doc_hash: [1.0, 2.0]},  # 2 values, but DIMS=4
        )


def test_insert_rejects_malformed_document_hash(db_path):
    build = _make_ready_build(db_path)
    with pytest.raises(ValueError):
        insert_embeddings(
            build.id, 1, "alice", PROVIDER, MODEL, DIMS,
            {"not-a-hash": _vec(1)},
        )


def test_insert_rejects_invalid_identity(db_path):
    build = _make_ready_build(db_path)
    doc_hash = _doc("dbo.learners").document_hash
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", "", MODEL, DIMS, {doc_hash: _vec(1)})
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, "", DIMS, {doc_hash: _vec(1)})
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, 0, {doc_hash: _vec(1)})
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, -1, {doc_hash: _vec(1)})


def test_insert_rejects_empty_batch(db_path):
    build = _make_ready_build(db_path)
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {})


# ── 5. First insert vs reuse ────────────────────────────────────────────────

def test_first_insert_creates_row_second_identical_call_reuses(db_path):
    build = _make_ready_build(db_path)
    doc_hash = _doc("dbo.learners").document_hash
    vector = _vec(1)

    first = insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: vector})
    assert first.status == STATUS_OK
    assert first.inserted_hashes == (doc_hash,)
    assert first.reused_hashes == ()

    second = insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: vector})
    assert second.status == STATUS_OK
    assert second.inserted_hashes == ()
    assert second.reused_hashes == (doc_hash,)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_embeddings").fetchone()[0]
    conn.close()
    assert count == 1  # never duplicated


# ── 6. Conflicting vector detection ────────────────────────────────────────

def test_conflicting_vector_for_same_key_is_rejected(db_path):
    build = _make_ready_build(db_path)
    doc_hash = _doc("dbo.learners").document_hash

    insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: _vec(1)})
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: _vec(2)})

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT vector_blob FROM sql_retrieval_embeddings").fetchall()
    conn.close()
    assert len(rows) == 1
    assert bytes(rows[0][0]) == serialize_vector(_vec(1))  # original preserved, not overwritten


# ── 7. Cross-build reuse for identical document hashes ─────────────────────

def test_identical_document_hash_reused_across_different_builds(db_path):
    build_a = _make_ready_build(db_path, source_id=1, snapshot_id=1, object_fqns=("dbo.learners",))
    doc_hash = _doc("dbo.learners").document_hash
    insert_embeddings(build_a.id, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: _vec(1)})

    # A second source whose one document happens to produce the exact same
    # document_hash (identical document_text) — the embedding must be
    # reused, not recomputed/duplicated, even though it's a different
    # source and a different build.
    build_b = _make_ready_build(db_path, source_id=2, snapshot_id=2, object_fqns=("dbo.learners",))
    result = insert_embeddings(build_b.id, 2, "alice", PROVIDER, MODEL, DIMS, {doc_hash: _vec(1)})
    assert result.status == STATUS_OK
    assert result.inserted_hashes == ()
    assert result.reused_hashes == (doc_hash,)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_embeddings").fetchone()[0]
    conn.close()
    assert count == 1

    fetched = get_embeddings_for_build(build_b.id, 2, "alice", PROVIDER, MODEL, DIMS)
    assert len(fetched) == 1
    assert fetched[0].document_hash == doc_hash


# ── 8. Unknown-document rejection ──────────────────────────────────────────

def test_insert_rejects_hash_not_in_build(db_path):
    build = _make_ready_build(db_path)
    foreign_hash = hashlib.sha256(b"some other document").hexdigest()
    with pytest.raises(ValueError):
        insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {foreign_hash: _vec(1)})

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_embeddings").fetchone()[0]
    conn.close()
    assert count == 0


# ── 9. READY-only enforcement ───────────────────────────────────────────────

def test_insert_and_read_reject_non_ready_build(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at) "
        "VALUES (1, 1, 1, 1, 1, ?, ?, ?, 'BUILDING', 0, '2026-09-04T00:00:00Z')",
        (hashlib.sha256(b"x").hexdigest(), hashlib.sha256(b"y").hexdigest(), hashlib.sha256(b"z").hexdigest()),
    )
    conn.commit()
    conn.close()

    doc_hash = hashlib.sha256(b"whatever").hexdigest()
    result = insert_embeddings(1, 1, "alice", PROVIDER, MODEL, DIMS, {doc_hash: _vec(1)})
    assert result.status == STATUS_BUILD_NOT_READY

    assert get_embeddings_for_build(1, 1, "alice", PROVIDER, MODEL, DIMS) == ()
    assert find_missing_document_hashes(1, 1, "alice", PROVIDER, MODEL, DIMS) is None
    completeness = get_embedding_build_completeness(1, 1, "alice", PROVIDER, MODEL, DIMS)
    assert completeness.status == STATUS_BUILD_NOT_READY
    assert completeness.is_complete is False
    assert completeness.total_documents == 0


def test_unowned_source_rejected(db_path):
    build = _make_ready_build(db_path, user_id="alice")
    doc_hash = _doc("dbo.learners").document_hash

    result = insert_embeddings(build.id, 1, "mallory", PROVIDER, MODEL, DIMS, {doc_hash: _vec(1)})
    assert result.status == STATUS_UNOWNED
    assert get_embeddings_for_build(build.id, 1, "mallory", PROVIDER, MODEL, DIMS) == ()
    assert find_missing_document_hashes(build.id, 1, "mallory", PROVIDER, MODEL, DIMS) is None
    completeness = get_embedding_build_completeness(build.id, 1, "mallory", PROVIDER, MODEL, DIMS)
    assert completeness.status == STATUS_UNOWNED


def test_build_not_found_for_source_rejected(db_path):
    build = _make_ready_build(db_path, source_id=1, snapshot_id=1)
    _insert_source(db_path, 2)  # a second, real, owned source with no builds

    result = insert_embeddings(build.id, 2, "alice", PROVIDER, MODEL, DIMS, {_doc("dbo.learners").document_hash: _vec(1)})
    assert result.status == STATUS_BUILD_NOT_FOUND
    assert get_embeddings_for_build(build.id, 2, "alice", PROVIDER, MODEL, DIMS) == ()


# ── 10. Missing-hash reporting ──────────────────────────────────────────────

def test_missing_hash_reporting_reflects_partial_embedding(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    learners_hash = _doc("dbo.learners").document_hash
    tracks_hash = _doc("dbo.tracks").document_hash

    insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {learners_hash: _vec(1)})

    missing = find_missing_document_hashes(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert missing == (tracks_hash,)


# ── 11. Complete / incomplete build reporting ──────────────────────────────

def test_completeness_reporting_incomplete_then_complete(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    learners_hash = _doc("dbo.learners").document_hash
    tracks_hash = _doc("dbo.tracks").document_hash

    incomplete = get_embedding_build_completeness(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert incomplete.status == STATUS_OK
    assert incomplete.total_documents == 2
    assert incomplete.embedded_document_count == 0
    assert incomplete.missing_document_hashes == tuple(sorted([learners_hash, tracks_hash]))
    assert incomplete.is_complete is False

    insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {learners_hash: _vec(1), tracks_hash: _vec(2)})

    complete = get_embedding_build_completeness(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert complete.total_documents == 2
    assert complete.embedded_document_count == 2
    assert complete.missing_document_hashes == ()
    assert complete.is_complete is True


# ── 12. Atomic rollback ─────────────────────────────────────────────────────

def test_atomic_rollback_on_conflict_within_batch(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    learners_hash = _doc("dbo.learners").document_hash
    tracks_hash = _doc("dbo.tracks").document_hash

    # Pre-seed a conflicting vector for tracks under a DIFFERENT vector so a
    # later batch that also tries to write tracks (with yet another vector)
    # alongside a brand-new learners vector must roll back entirely.
    insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, {tracks_hash: _vec(99)})

    with pytest.raises(ValueError):
        insert_embeddings(
            build.id, 1, "alice", PROVIDER, MODEL, DIMS,
            {learners_hash: _vec(1), tracks_hash: _vec(2)},  # tracks conflicts
        )

    # learners must NOT have been inserted despite being processed first —
    # the whole batch rolled back, not just the conflicting entry.
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT document_hash FROM sql_retrieval_embeddings"
    ).fetchall()
    conn.close()
    assert {r[0] for r in rows} == {tracks_hash}


def test_atomic_rollback_on_unknown_hash_within_batch(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.learners", "dbo.tracks"))
    learners_hash = _doc("dbo.learners").document_hash
    foreign_hash = hashlib.sha256(b"unrelated").hexdigest()

    with pytest.raises(ValueError):
        insert_embeddings(
            build.id, 1, "alice", PROVIDER, MODEL, DIMS,
            {learners_hash: _vec(1), foreign_hash: _vec(2)},
        )

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_embeddings").fetchone()[0]
    conn.close()
    assert count == 0  # nothing committed, including the valid learners hash


# ── 13. Source/build isolation ──────────────────────────────────────────────

def test_completeness_scoped_to_its_own_build_not_another_sharing_a_hash(db_path):
    build_a = _make_ready_build(db_path, source_id=1, snapshot_id=1, object_fqns=("dbo.learners",))
    build_b = _make_ready_build(db_path, source_id=2, snapshot_id=2, object_fqns=("dbo.learners", "dbo.tracks"))
    learners_hash = _doc("dbo.learners").document_hash

    insert_embeddings(build_a.id, 1, "alice", PROVIDER, MODEL, DIMS, {learners_hash: _vec(1)})

    # build_b shares the learners hash (already cached via build_a, reused
    # content-addressed) but ALSO has its own unembedded tracks document —
    # completeness must reflect its OWN two documents (one embedded via
    # reuse, one still missing), not build_a's single-document build.
    completeness_b = get_embedding_build_completeness(build_b.id, 2, "alice", PROVIDER, MODEL, DIMS)
    assert completeness_b.total_documents == 2
    assert completeness_b.embedded_document_count == 1  # learners: reused from build_a's cached vector
    assert completeness_b.missing_document_hashes == (_doc("dbo.tracks").document_hash,)
    assert completeness_b.is_complete is False

    # get_embeddings_for_build for build_a must never include build_b's documents.
    fetched_a = get_embeddings_for_build(build_a.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert {e.object_fqn for e in fetched_a} == {"dbo.learners"}


def test_cannot_read_another_sources_build_by_guessing_its_id(db_path):
    build_a = _make_ready_build(db_path, source_id=1, snapshot_id=1)
    _insert_source(db_path, 2, user_id="bob")

    # bob owns source 2 but tries to read source 1's build by id.
    assert get_embeddings_for_build(build_a.id, 2, "bob", PROVIDER, MODEL, DIMS) == ()
    result = insert_embeddings(
        build_a.id, 2, "bob", PROVIDER, MODEL, DIMS,
        {_doc("dbo.learners").document_hash: _vec(1)},
    )
    assert result.status == STATUS_BUILD_NOT_FOUND


# ── 14. Deterministic retrieval ordering ────────────────────────────────────

def test_get_embeddings_for_build_orders_by_object_fqn(db_path):
    build = _make_ready_build(db_path, object_fqns=("dbo.zzz_last", "dbo.aaa_first", "dbo.mmm_mid"))
    hashes = {fqn: _doc(fqn).document_hash for fqn in ("dbo.zzz_last", "dbo.aaa_first", "dbo.mmm_mid")}
    vectors = {h: _vec(i) for i, h in enumerate(hashes.values())}

    insert_embeddings(build.id, 1, "alice", PROVIDER, MODEL, DIMS, vectors)

    fetched = get_embeddings_for_build(build.id, 1, "alice", PROVIDER, MODEL, DIMS)
    assert [e.object_fqn for e in fetched] == ["dbo.aaa_first", "dbo.mmm_mid", "dbo.zzz_last"]


# ── 15. Migration idempotency and model/migration equivalence ─────────────

def test_migration_013_is_idempotent(tmp_path):
    path = str(tmp_path / "idempotent.db")
    conn = sqlite3.connect(path)
    with open(MIGRATION_012_PATH, "r", encoding="utf-8") as f:
        prereq_012 = f.read()
    conn.executescript("""
        CREATE TABLE data_source_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            display_name TEXT NOT NULL, source_type TEXT NOT NULL,
            source_category TEXT NOT NULL, encrypted_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            snapshot_version INTEGER NOT NULL, source_type TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, discovered_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.executescript(prereq_012)
    conn.commit()

    with open(MIGRATION_013_PATH, "r", encoding="utf-8") as f:
        migration_sql = f.read()

    conn.executescript(migration_sql)
    conn.commit()
    conn.executescript(migration_sql)  # second run must not error
    conn.commit()

    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sql_retrieval_embeddings'"
        ).fetchall()
    }
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_sre_%'"
        ).fetchall()
    }
    conn.close()

    assert tables == {"sql_retrieval_embeddings"}
    assert indexes == {"idx_sre_provider_model_dims_hash", "idx_sre_hash"}


def _table_schema_signature(db_path, table_name):
    conn = sqlite3.connect(db_path)
    try:
        columns = [
            (row[1], row[2], row[3], row[4], row[5])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        index_list = sorted(
            (row[1], bool(row[2]))
            for row in conn.execute(f"PRAGMA index_list({table_name})").fetchall()
        )
        index_columns = {
            name: [r[2] for r in conn.execute(f"PRAGMA index_info({name})").fetchall()]
            for name, _ in index_list
        }
        return columns, index_list, index_columns
    finally:
        conn.close()


def test_migration_ddl_and_models_ddl_remain_equivalent(tmp_path, monkeypatch):
    init_db_path = str(tmp_path / "via_init_db.db")
    original_get_connection = models_module.get_connection
    monkeypatch.setattr(models_module, "get_connection", lambda: _make_connection(init_db_path))
    models_module.init_db()
    monkeypatch.setattr(models_module, "get_connection", original_get_connection)

    migration_db_path = str(tmp_path / "via_migration.db")
    conn = sqlite3.connect(migration_db_path)
    conn.executescript("""
        CREATE TABLE data_source_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            display_name TEXT NOT NULL, source_type TEXT NOT NULL,
            source_category TEXT NOT NULL, encrypted_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            snapshot_version INTEGER NOT NULL, source_type TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, discovered_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    with open(MIGRATION_012_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    with open(MIGRATION_013_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()

    via_init_db = _table_schema_signature(init_db_path, "sql_retrieval_embeddings")
    via_migration = _table_schema_signature(migration_db_path, "sql_retrieval_embeddings")
    assert via_init_db == via_migration


def test_migration_013_does_not_alter_existing_tables_or_rows(tmp_path):
    path = str(tmp_path / "migration_preserve.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE data_source_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
            display_name TEXT NOT NULL, source_type TEXT NOT NULL,
            source_category TEXT NOT NULL, encrypted_config_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            snapshot_version INTEGER NOT NULL, source_type TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, discovered_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (1, 'alice', 'Existing', 'mssql', 'database', '{}', 'now', 'now')"
    )
    conn.commit()
    with open(MIGRATION_012_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    before = conn.execute("SELECT * FROM data_source_connections").fetchall()

    with open(MIGRATION_013_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    after = conn.execute("SELECT * FROM data_source_connections").fetchall()
    conn.close()
    assert before == after


# ── DB-level CHECK constraints (defense in depth beneath the app layer) ───

def test_db_check_constraint_rejects_dimension_length_mismatch(db_path):
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sql_retrieval_embeddings "
            "(embedding_provider, embedding_model, embedding_dimensions, document_hash, "
            " vector_blob, vector_byte_length, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (PROVIDER, MODEL, 4, hashlib.sha256(b"x").hexdigest(), b"\x00" * 8, 8, "now"),
        )
    conn.close()
