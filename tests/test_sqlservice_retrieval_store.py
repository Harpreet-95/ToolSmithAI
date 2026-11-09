"""
Focused unit tests — SQL rebuild Phase 1, Step 5: transactional SQLite
persistence for retrieval-document builds (core/sqlservice/retrieval_store.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server, never OpenAI. Schema is created via
the real `data.models.init_db()` (monkeypatched to a temp file) so these
tests exercise the actual production schema, not a hand-maintained copy.

Run:
    venv/Scripts/pytest tests/test_sqlservice_retrieval_store.py -v
"""

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import core.sqlservice.retrieval_store as retrieval_store_module
from core.sqlservice.metadata_loader import RetrievalDocumentBatch, STATUS_READY
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.retrieval_store import (
    DOCUMENT_SET_HASH_VERSION,
    OUTCOME_CREATED,
    OUTCOME_REUSED,
    OUTCOME_UNOWNED,
    compute_document_set_hash,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
    persist_retrieval_build,
)


# ── Fixture plumbing ──────────────────────────────────────────────────────

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "migrations", "012_create_sql_retrieval_store.sql",
)


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _patch_retrieval_store_connection(monkeypatch, db_path):
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(db_path))


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    """
    Create the FULL real schema (including our new tables) exactly once
    per module, via the actual data.models.init_db() — not a
    hand-maintained copy. init_db() does dozens of individual commits
    across ~50 tables, which is slow to repeat per-test, so every test
    instead gets a fast file copy of this one template (see db_path
    below); the schema itself still comes entirely from the real
    init_db(), so fidelity is unchanged.
    """
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
    _patch_retrieval_store_connection(monkeypatch, path)
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


def _fingerprint(*, version=1, fingerprint_hash=None, table_set_hash=None, table_fqns=None):
    if fingerprint_hash is None:
        fingerprint_hash = hashlib.sha256(b"fingerprint-a").hexdigest()
    if table_set_hash is None:
        table_set_hash = hashlib.sha256(b"table-set-a").hexdigest()
    result = {
        "fingerprint_version": version,
        "fingerprint_hash": fingerprint_hash,
        "table_set_hash": table_set_hash,
    }
    if table_fqns is not None:
        result["table_fqns"] = table_fqns
    return result


class _TrackingConnection:
    """Wraps a real sqlite3.Connection to record close() calls."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.closed = False

    def close(self):
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FailingExecutemanyConnection:
    """Wraps a real sqlite3.Connection whose executemany() always raises —
    used to simulate a mid-insert failure for the rollback tests."""

    def __init__(self, real_conn):
        self._real = real_conn
        self.closed = False

    def executemany(self, *args, **kwargs):
        raise sqlite3.OperationalError("injected failure for test_mid_insert_failure")

    def close(self):
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── 1-2. READY batch persists; all documents stored ──────────────────────

def test_ready_batch_persists_successfully_and_stores_all_documents(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.learners"), _doc("dbo.tracks")]
    batch = _batch(1, 1, 1, docs)

    result = persist_retrieval_build(batch, _fingerprint(), "alice")

    assert result.outcome == OUTCOME_CREATED
    assert result.build is not None
    assert result.build.status == "READY"
    assert result.build.document_count == 2
    assert result.build.source_id == 1
    assert result.build.snapshot_id == 1

    stored = load_stored_retrieval_documents(result.build.id, 1, "alice")
    assert len(stored) == 2
    assert {d.object_fqn for d in stored} == {"dbo.learners", "dbo.tracks"}


# ── 3. Metadata JSON canonical + round-trips immutably ────────────────────

def test_metadata_round_trips_canonically_and_immutably(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    metadata = {"b_key": 2, "a_key": 1, "nested": {"z": 1, "a": 2}}
    docs = [_doc("dbo.learners", metadata=metadata)]
    batch = _batch(1, 1, 1, docs)

    result = persist_retrieval_build(batch, _fingerprint(), "alice")
    stored = load_stored_retrieval_documents(result.build.id, 1, "alice")[0]

    assert dict(stored.metadata) == metadata
    with pytest.raises(TypeError):
        stored.metadata["new_key"] = "nope"

    # Canonical JSON: sorted keys, compact separators.
    conn = sqlite3.connect(db_path)
    raw = conn.execute(
        "SELECT metadata_json FROM sql_retrieval_documents WHERE build_id = ?", (result.build.id,)
    ).fetchone()[0]
    conn.close()
    assert raw == json.dumps(metadata, sort_keys=True, separators=(",", ":"))


# ── 3b. Step 5A: deep (recursive) immutability ─────────────────────────────

def test_nested_mappings_and_sequences_are_deeply_immutable(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    metadata = {
        "top": "value",
        "nested_dict": {"inner": 1, "deeper": {"deepest": "x"}},
        "nested_list": [1, 2, {"in_list": "y"}],
    }
    result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners", metadata=metadata)]), _fingerprint(), "alice")
    stored = load_stored_retrieval_documents(result.build.id, 1, "alice")[0]

    # Round-trips correctly.
    assert stored.metadata["top"] == "value"
    assert stored.metadata["nested_dict"]["inner"] == 1
    assert stored.metadata["nested_dict"]["deeper"]["deepest"] == "x"
    assert stored.metadata["nested_list"] == (1, 2, {"in_list": "y"})

    # Top level immutable.
    with pytest.raises(TypeError):
        stored.metadata["top"] = "changed"

    # Nested dict immutable.
    with pytest.raises(TypeError):
        stored.metadata["nested_dict"]["inner"] = 999

    # Doubly-nested dict immutable.
    with pytest.raises(TypeError):
        stored.metadata["nested_dict"]["deeper"]["deepest"] = "changed"

    # Lists become tuples (no .append/mutation possible), and a dict nested
    # inside a list is itself read-only.
    assert isinstance(stored.metadata["nested_list"], tuple)
    with pytest.raises(AttributeError):
        stored.metadata["nested_list"].append(4)
    with pytest.raises(TypeError):
        stored.metadata["nested_list"][2]["in_list"] = "changed"


# ── 4. Document-set hash order independence ───────────────────────────────

def test_document_set_hash_is_order_independent():
    docs_a = [_doc("dbo.a"), _doc("dbo.b"), _doc("dbo.c")]
    docs_b = list(reversed(docs_a))
    assert compute_document_set_hash(docs_a) == compute_document_set_hash(docs_b)


# ── 5. Identical build returns REUSED, no duplicate rows ─────────────────

def test_identical_build_returns_reused_without_duplicate_rows(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.learners")]
    batch = _batch(1, 1, 1, docs)
    fingerprint = _fingerprint()

    first = persist_retrieval_build(batch, fingerprint, "alice")
    second = persist_retrieval_build(batch, fingerprint, "alice")

    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_REUSED
    assert second.build.id == first.build.id

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    doc_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_documents WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1
    assert doc_count == 1


# ── 6-7. A real change always creates a new build ─────────────────────────

def test_changed_document_hash_creates_new_build(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    fingerprint = _fingerprint()

    first = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners", text="v1")]), fingerprint, "alice")
    second = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners", text="v2")]), fingerprint, "alice")

    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_CREATED
    assert second.build.id != first.build.id


def test_changed_fingerprint_creates_new_build(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.learners")]

    first = persist_retrieval_build(_batch(1, 1, 1, docs), _fingerprint(), "alice")
    second = persist_retrieval_build(
        _batch(1, 1, 1, docs),
        _fingerprint(fingerprint_hash=hashlib.sha256(b"fingerprint-b").hexdigest()),
        "alice",
    )

    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_CREATED
    assert second.build.id != first.build.id


# ── Step 5A: fingerprint_version-aware reuse ──────────────────────────────

def test_reuse_requires_matching_fingerprint_version(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.learners")]

    fp_v1 = _fingerprint(version=1)
    first = persist_retrieval_build(_batch(1, 1, 1, docs), fp_v1, "alice")
    assert first.outcome == OUTCOME_CREATED

    # Same source/schema_fingerprint/document_set_hash, matching version -> REUSED.
    same_version_again = persist_retrieval_build(_batch(1, 1, 1, docs), fp_v1, "alice")
    assert same_version_again.outcome == OUTCOME_REUSED
    assert same_version_again.build.id == first.build.id


def test_different_fingerprint_version_does_not_reuse_and_creates_new_build(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.learners")]
    # Same fingerprint_hash/table_set_hash string, only the VERSION differs.
    fp_v1 = _fingerprint(version=1, fingerprint_hash=hashlib.sha256(b"same-hash").hexdigest())
    fp_v2 = _fingerprint(version=2, fingerprint_hash=hashlib.sha256(b"same-hash").hexdigest())

    first = persist_retrieval_build(_batch(1, 1, 1, docs), fp_v1, "alice")
    second = persist_retrieval_build(_batch(1, 1, 1, docs), fp_v2, "alice")

    assert first.outcome == OUTCOME_CREATED
    assert second.outcome == OUTCOME_CREATED  # NOT reused despite identical hash strings
    assert second.build.id != first.build.id
    assert second.build.fingerprint_version == 2

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert count == 2


# ── 8-14. Pre-write validation ─────────────────────────────────────────────

def test_non_ready_batch_rejected_before_writes(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [_doc("dbo.learners")], status="NO_SNAPSHOT")
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds").fetchone()[0]
    conn.close()
    assert count == 0


def test_empty_batch_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


def test_duplicate_fqns_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [_doc("dbo.learners", text="a"), _doc("dbo.learners", text="b")])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


def test_mismatched_document_source_id_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    bad_doc = _doc("dbo.learners", source_id=999)
    batch = _batch(1, 1, 1, [bad_doc])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


@pytest.mark.parametrize(
    "make_doc",
    [
        lambda: dataclasses.replace(_doc("dbo.learners"), document_hash="not-a-hash"),
        lambda: dataclasses.replace(_doc("dbo.learners"), retrieval_tier=5),
        lambda: dataclasses.replace(_doc("dbo.learners"), retrieval_tier=0),
        lambda: dataclasses.replace(_doc("dbo.learners"), object_type="PROCEDURE"),
    ],
)
def test_invalid_hash_tier_or_object_type_rejected(db_path, make_doc):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [make_doc()])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


def test_non_json_safe_metadata_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    bad_doc = _doc("dbo.learners", metadata={"bad": {1, 2, 3}})  # a set is not JSON-safe
    batch = _batch(1, 1, 1, [bad_doc])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


def test_fingerprint_table_fqns_mismatch_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [_doc("dbo.learners"), _doc("dbo.tracks")])
    fingerprint = _fingerprint(table_fqns=["dbo.learners", "dbo.SOMETHING_ELSE"])
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, fingerprint, "alice")


def test_fingerprint_table_fqns_matching_is_accepted(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [_doc("dbo.learners"), _doc("dbo.tracks")])
    fingerprint = _fingerprint(table_fqns=["dbo.tracks", "dbo.learners"])  # order-independent
    result = persist_retrieval_build(batch, fingerprint, "alice")
    assert result.outcome == OUTCOME_CREATED


# ── 15. Snapshot must belong to the same source ───────────────────────────

def test_snapshot_not_belonging_to_source_is_rejected(db_path):
    _insert_source(db_path, 1)
    _insert_source(db_path, 2, user_id="bob")
    _insert_snapshot(db_path, 1, source_id=2)  # snapshot belongs to source 2
    batch = _batch(1, 1, 1, [_doc("dbo.learners")])  # but batch claims source 1
    with pytest.raises(ValueError):
        persist_retrieval_build(batch, _fingerprint(), "alice")


# ── 16-18. Ownership protections ──────────────────────────────────────────

def test_unowned_source_cannot_persist(db_path):
    _insert_source(db_path, 1, user_id="alice")
    _insert_snapshot(db_path, 1, source_id=1)
    batch = _batch(1, 1, 1, [_doc("dbo.learners")])
    result = persist_retrieval_build(batch, _fingerprint(), "mallory")
    assert result.outcome == OUTCOME_UNOWNED
    assert result.build is None
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds").fetchone()[0]
    conn.close()
    assert count == 0


def test_unowned_source_cannot_read_build(db_path):
    _insert_source(db_path, 1, user_id="alice")
    _insert_snapshot(db_path, 1, source_id=1)
    persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")
    assert get_latest_ready_retrieval_build(1, "mallory") is None
    assert get_latest_ready_retrieval_build(1, "alice") is not None


def test_unowned_source_cannot_read_documents(db_path):
    _insert_source(db_path, 1, user_id="alice")
    _insert_snapshot(db_path, 1, source_id=1)
    result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")
    assert load_stored_retrieval_documents(result.build.id, 1, "mallory") == ()
    assert len(load_stored_retrieval_documents(result.build.id, 1, "alice")) == 1


# ── 19. Tier filtering ─────────────────────────────────────────────────────

def test_tier_filtering_works(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [
        _doc("dbo.a", tier=1), _doc("dbo.b", tier=2), _doc("dbo.c", tier=3), _doc("dbo.d", tier=4),
    ]
    result = persist_retrieval_build(_batch(1, 1, 1, docs), _fingerprint(), "alice")

    tier1_only = load_stored_retrieval_documents(result.build.id, 1, "alice", tiers=(1,))
    assert {d.object_fqn for d in tier1_only} == {"dbo.a"}

    tier1_and_2 = load_stored_retrieval_documents(result.build.id, 1, "alice", tiers=(1, 2))
    assert {d.object_fqn for d in tier1_and_2} == {"dbo.a", "dbo.b"}

    all_docs = load_stored_retrieval_documents(result.build.id, 1, "alice")
    assert len(all_docs) == 4

    with pytest.raises(ValueError):
        load_stored_retrieval_documents(result.build.id, 1, "alice", tiers=(9,))


# ── Step 5A: READY-only reads ──────────────────────────────────────────────
#
# persist_retrieval_build() never leaves a BUILDING/FAILED row committed and
# visible (it always fully rolls back on any failure), so these two rows
# are inserted directly via raw SQL — the only way to construct that state
# for a test — to prove load_stored_retrieval_documents() refuses them.

def _insert_build_and_document_directly(db_path, build_id, source_id, status):
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (?, ?, 1, 1, 1, ?, ?, ?, ?, 1, ?, ?)",
        (
            build_id, source_id, hashlib.sha256(b"fp").hexdigest(),
            hashlib.sha256(b"ts").hexdigest(), hashlib.sha256(b"ds").hexdigest(),
            status, now, now if status != "BUILDING" else None,
        ),
    )
    conn.execute(
        "INSERT INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, "
        " document_text, metadata_json, created_at) "
        "VALUES (?, ?, 'dbo.in_progress', 'TABLE', 1, ?, 'text', '{}', ?)",
        (build_id, source_id, hashlib.sha256(b"x").hexdigest(), now),
    )
    conn.commit()
    conn.close()


def test_documents_from_building_build_cannot_be_loaded(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    _insert_build_and_document_directly(db_path, build_id=101, source_id=1, status="BUILDING")

    assert load_stored_retrieval_documents(101, 1, "alice") == ()


def test_documents_from_failed_build_cannot_be_loaded(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    _insert_build_and_document_directly(db_path, build_id=102, source_id=1, status="FAILED")

    assert load_stored_retrieval_documents(102, 1, "alice") == ()


def test_documents_from_ready_build_load_normally(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    _insert_build_and_document_directly(db_path, build_id=103, source_id=1, status="READY")

    loaded = load_stored_retrieval_documents(103, 1, "alice")
    assert len(loaded) == 1
    assert loaded[0].object_fqn == "dbo.in_progress"


# ── 20-22. Rollback guarantees ─────────────────────────────────────────────

def test_mid_insert_failure_rolls_back_entire_build_and_preserves_prior_ready_build(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)

    # A genuine prior READY build.
    good_fingerprint = _fingerprint()
    good_result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), good_fingerprint, "alice")
    assert good_result.outcome == OUTCOME_CREATED

    # Force the next attempt (different fingerprint -> CREATED path, not REUSED)
    # to fail mid-insert.
    def _failing_get_connection():
        real = sqlite3.connect(db_path)
        real.row_factory = sqlite3.Row
        real.execute("PRAGMA foreign_keys = ON")
        return _FailingExecutemanyConnection(real)

    monkeypatch.setattr(retrieval_store_module, "get_connection", _failing_get_connection)

    bad_fingerprint = _fingerprint(fingerprint_hash=hashlib.sha256(b"fingerprint-fail").hexdigest())
    with pytest.raises(sqlite3.OperationalError):
        persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.other")]), bad_fingerprint, "alice")

    conn = sqlite3.connect(db_path)
    builds = conn.execute("SELECT id, status, schema_fingerprint FROM sql_retrieval_builds WHERE source_id = 1").fetchall()
    docs_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_documents WHERE source_id = 1").fetchone()[0]
    conn.close()

    # Only the original good build survives — no partial/failed row, and no
    # BUILDING/READY row for the failed attempt.
    assert len(builds) == 1
    assert builds[0][0] == good_result.build.id
    assert builds[0][1] == "READY"
    assert builds[0][2] == good_fingerprint["fingerprint_hash"]
    assert docs_count == 1


# ── Step 5A: database-enforced uniqueness / concurrency-conflict recovery ──

def test_database_uniqueness_prevents_duplicate_logical_builds(db_path):
    """Direct proof the UNIQUE index exists and is enforced, independent
    of persist_retrieval_build()'s own application-level reuse check."""
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    now = "2026-09-04T00:00:00Z"
    insert_sql = (
        "INSERT INTO sql_retrieval_builds "
        "(source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at) "
        "VALUES (1, 1, 1, 1, 'fp-hash', 'ts-hash', 'ds-hash', 'READY', 0, ?)"
    )
    conn = sqlite3.connect(db_path)
    conn.execute(insert_sql, (now,))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert_sql, (now,))
    conn.close()


class _RaceSimulatingConnection:
    """Wraps a real sqlite3.Connection. On the first INSERT into
    sql_retrieval_builds it receives, first commits a conflicting READY
    row via a SEPARATE connection (simulating a concurrent winner that
    committed between our own reuse-check and our own INSERT), then lets
    the real INSERT proceed and hit the UNIQUE constraint for real — this
    exercises persist_retrieval_build()'s actual IntegrityError-recovery
    branch, not just a mocked-out shortcut."""

    def __init__(self, real_conn, db_path, conflicting_sql, conflicting_params):
        self._real = real_conn
        self._db_path = db_path
        self._conflicting_sql = conflicting_sql
        self._conflicting_params = conflicting_params
        self._injected = False

    def execute(self, sql, params=()):
        if not self._injected and sql.strip().upper().startswith("INSERT INTO SQL_RETRIEVAL_BUILDS"):
            self._injected = True
            side_conn = sqlite3.connect(self._db_path)
            side_conn.execute(self._conflicting_sql, self._conflicting_params)
            side_conn.commit()
            side_conn.close()
        return self._real.execute(sql, params)

    def executemany(self, sql, rows):
        return self._real.executemany(sql, rows)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_uniqueness_conflict_is_recovered_as_reused(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    fingerprint = _fingerprint()
    docs = [_doc("dbo.learners")]
    document_set_hash = compute_document_set_hash(docs)
    now = "2026-09-04T00:00:00Z"

    conflicting_sql = (
        "INSERT INTO sql_retrieval_builds "
        "(source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (1, 1, 1, ?, ?, ?, ?, 'READY', 1, ?, ?)"
    )
    conflicting_params = (
        fingerprint["fingerprint_version"], fingerprint["fingerprint_hash"],
        fingerprint["table_set_hash"], document_set_hash, now, now,
    )

    def _fake_get_connection():
        real = _make_connection(db_path)
        return _RaceSimulatingConnection(real, db_path, conflicting_sql, conflicting_params)

    monkeypatch.setattr(retrieval_store_module, "get_connection", _fake_get_connection)

    result = persist_retrieval_build(_batch(1, 1, 1, docs), fingerprint, "alice")

    assert result.outcome == OUTCOME_REUSED
    assert result.build is not None
    assert result.build.status == "READY"

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1  # only the simulated winner — our own attempt rolled back, not duplicated


class _UnrelatedIntegrityErrorConnection:
    """Wraps a real sqlite3.Connection whose executemany() always raises an
    IntegrityError unrelated to the build-uniqueness index."""

    def __init__(self, real_conn):
        self._real = real_conn

    def executemany(self, *args, **kwargs):
        raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_unrelated_integrity_error_is_not_converted_to_reused(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)

    def _fake_get_connection():
        return _UnrelatedIntegrityErrorConnection(_make_connection(db_path))

    monkeypatch.setattr(retrieval_store_module, "get_connection", _fake_get_connection)

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")


# ── 23. Connections close on success and failure ──────────────────────────

def test_connection_closes_on_success(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    created: list = []

    def _fake_get_connection():
        real = _make_connection(db_path)
        wrapper = _TrackingConnection(real)
        created.append(wrapper)
        return wrapper

    monkeypatch.setattr(retrieval_store_module, "get_connection", _fake_get_connection)

    result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")
    assert result.outcome == OUTCOME_CREATED
    assert created[0].closed is True


def test_connection_closes_after_snapshot_mismatch_failure(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_source(db_path, 2, user_id="bob")
    _insert_snapshot(db_path, 1, source_id=2)  # wrong source
    created: list = []

    def _fake_get_connection():
        real = _make_connection(db_path)
        wrapper = _TrackingConnection(real)
        created.append(wrapper)
        return wrapper

    monkeypatch.setattr(retrieval_store_module, "get_connection", _fake_get_connection)

    with pytest.raises(ValueError):
        persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")
    assert created[0].closed is True


# ── 24-25. Cascade behavior ─────────────────────────────────────────────

def test_source_deletion_cascades_to_builds_and_documents(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners")]), _fingerprint(), "alice")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM data_source_connections WHERE id = 1")
    conn.commit()
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE id = ?", (result.build.id,)).fetchone()[0]
    doc_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_documents WHERE build_id = ?", (result.build.id,)).fetchone()[0]
    conn.close()
    assert build_count == 0
    assert doc_count == 0


def test_build_deletion_cascades_to_documents(db_path):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    result = persist_retrieval_build(_batch(1, 1, 1, [_doc("dbo.learners"), _doc("dbo.tracks")]), _fingerprint(), "alice")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DELETE FROM sql_retrieval_builds WHERE id = ?", (result.build.id,))
    conn.commit()
    doc_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_documents WHERE build_id = ?", (result.build.id,)).fetchone()[0]
    conn.close()
    assert doc_count == 0


# ── 26. Fresh init_db() creates both tables and indexes ──────────────────

def test_fresh_init_db_creates_tables_and_indexes(db_path):
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sql_retrieval_%'"
        ).fetchall()
    }
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_sr%'"
        ).fetchall()
    }
    conn.close()

    # Step 5 created sql_retrieval_builds/sql_retrieval_documents; Phase 2
    # Step 1 (core/sqlservice/embedding_store.py) additively created
    # sql_retrieval_embeddings alongside them — still matched by this same
    # 'sql_retrieval_%' LIKE pattern used by a full init_db() run.
    assert tables == {"sql_retrieval_builds", "sql_retrieval_documents", "sql_retrieval_embeddings"}
    expected_indexes = {
        "idx_srb_source_status", "idx_srb_unique_build",
        "idx_srd_build_fqn", "idx_srd_build_id", "idx_srd_build_tier", "idx_srd_hash",
    }
    assert expected_indexes.issubset(indexes)


# ── 27-28. Migration compatibility ─────────────────────────────────────────

def test_migration_creates_compatible_schema_on_existing_database(tmp_path):
    """A database that only has the prerequisite tables (as if this
    migration were the first time sql_retrieval_* is introduced) ends up
    with the identical two tables/indexes after running the migration
    file, matching a fresh init_db()."""
    path = str(tmp_path / "migration_target.db")
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
    conn.commit()

    with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
        migration_sql = f.read()
    conn.executescript(migration_sql)
    conn.commit()

    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'sql_retrieval_%'"
        ).fetchall()
    }
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_sr%'"
        ).fetchall()
    }
    conn.close()

    assert tables == {"sql_retrieval_builds", "sql_retrieval_documents"}
    expected_indexes = {
        "idx_srb_source_status", "idx_srb_unique_build",
        "idx_srd_build_fqn", "idx_srd_build_id", "idx_srd_build_tier", "idx_srd_hash",
    }
    assert expected_indexes.issubset(indexes)


def _table_schema_signature(db_path, table_name):
    """Structural signature (columns + indexes + which are unique + their
    indexed columns) — compared instead of raw SQL text, since
    data/models.py and the migration file intentionally carry different
    comments even though their CREATE statements must be equivalent."""
    conn = sqlite3.connect(db_path)
    try:
        columns = [
            (row[1], row[2], row[3], row[4], row[5])  # name, type, notnull, dflt_value, pk
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        ]
        index_list = sorted(
            (row[1], bool(row[2]))  # index name, unique
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
    """The exact requirement from Step 5A: the UNIQUE-index change (and
    every other column/constraint) must be identical whether a database
    reaches this schema via a fresh data.models.init_db() or via applying
    migrations/012_create_sql_retrieval_store.sql on top of only the
    prerequisite tables."""
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
    with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()

    for table_name in ("sql_retrieval_builds", "sql_retrieval_documents"):
        via_init_db = _table_schema_signature(init_db_path, table_name)
        via_migration = _table_schema_signature(migration_db_path, table_name)
        assert via_init_db == via_migration, f"{table_name} schema differs between init_db() and the migration"


def test_migration_does_not_alter_existing_tables_or_rows(tmp_path):
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
    conn.execute(
        "INSERT INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, 1, 1, 'mssql', '{}', 'now', 'now')"
    )
    conn.commit()

    before_source = conn.execute("SELECT * FROM data_source_connections").fetchall()
    before_snapshot = conn.execute("SELECT * FROM schema_snapshots").fetchall()

    with open(MIGRATION_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()

    after_source = conn.execute("SELECT * FROM data_source_connections").fetchall()
    after_snapshot = conn.execute("SELECT * FROM schema_snapshots").fetchall()
    conn.close()

    assert before_source == after_source
    assert before_snapshot == after_snapshot


# ── Step 4B: versioned document-set hash (includes metadata) ──────────────


def _old_v1_document_set_hash(documents):
    """Exact replica of the PRE-Step-4B compute_document_set_hash() formula
    — (object_fqn, document_hash) pairs only, no metadata, no version
    separator — used only to simulate a build persisted before this step,
    never imported from production code."""
    pairs = sorted((d.object_fqn, d.document_hash) for d in documents)
    canonical = json.dumps(pairs, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_document_set_hash_version_is_2():
    assert DOCUMENT_SET_HASH_VERSION == 2


def test_identical_documents_and_metadata_produce_identical_hashes():
    docs_a = [_doc("dbo.a", metadata={"business_name": "Alpha", "synonyms": ("x", "y")})]
    docs_b = [_doc("dbo.a", metadata={"business_name": "Alpha", "synonyms": ("x", "y")})]
    assert compute_document_set_hash(docs_a) == compute_document_set_hash(docs_b)


def test_document_order_does_not_matter_with_metadata():
    docs = [
        _doc("dbo.a", metadata={"business_name": "Alpha"}),
        _doc("dbo.b", metadata={"business_name": "Beta"}),
        _doc("dbo.c", metadata={"business_name": "Gamma"}),
    ]
    reversed_docs = list(reversed(docs))
    assert compute_document_set_hash(docs) == compute_document_set_hash(reversed_docs)


def test_mapping_key_insertion_order_does_not_matter():
    docs_a = [_doc("dbo.a", metadata={"b_key": 2, "a_key": 1})]
    docs_b = [_doc("dbo.a", metadata={"a_key": 1, "b_key": 2})]  # same content, different insertion order
    assert compute_document_set_hash(docs_a) == compute_document_set_hash(docs_b)


def test_metadata_only_change_changes_the_set_hash():
    docs_a = [_doc("dbo.a", metadata={"business_name": "Alpha"})]
    docs_b = [_doc("dbo.a", metadata={"business_name": "Beta"})]  # same object_fqn/document_hash
    assert docs_a[0].document_hash == docs_b[0].document_hash
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_tier_only_change_changes_the_set_hash():
    docs_a = [_doc("dbo.a", tier=1, text="same text")]
    docs_b = [_doc("dbo.a", tier=2, text="same text")]
    assert docs_a[0].document_hash == docs_b[0].document_hash
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_object_type_change_changes_the_set_hash():
    docs_a = [_doc("dbo.a", object_type="TABLE", text="same text")]
    docs_b = [_doc("dbo.a", object_type="VIEW", text="same text")]
    assert docs_a[0].document_hash == docs_b[0].document_hash
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_document_text_and_hash_change_changes_the_set_hash():
    docs_a = [_doc("dbo.a", text="version one")]
    docs_b = [_doc("dbo.a", text="version two")]
    assert docs_a[0].document_hash != docs_b[0].document_hash
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_nested_metadata_canonicalization_is_order_independent():
    docs_a = [_doc("dbo.a", metadata={"outer": {"z": 1, "a": [1, 2, {"y": 1, "x": 2}]}})]
    docs_b = [_doc("dbo.a", metadata={"outer": {"a": [1, 2, {"x": 2, "y": 1}], "z": 1}})]
    assert compute_document_set_hash(docs_a) == compute_document_set_hash(docs_b)


def test_nested_metadata_content_change_changes_the_hash():
    docs_a = [_doc("dbo.a", metadata={"outer": {"a": [1, 2, 3]}})]
    docs_b = [_doc("dbo.a", metadata={"outer": {"a": [1, 2, 4]}})]
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_sequence_order_within_metadata_is_preserved_not_resorted():
    docs_a = [_doc("dbo.a", metadata={"synonyms": ("alpha", "beta")})]
    docs_b = [_doc("dbo.a", metadata={"synonyms": ("beta", "alpha")})]
    assert compute_document_set_hash(docs_a) != compute_document_set_hash(docs_b)


def test_unsupported_metadata_value_rejected_not_stringified():
    docs = [_doc("dbo.a", metadata={"bad": {1, 2, 3}})]  # a set — not JSON-representable
    with pytest.raises(ValueError):
        compute_document_set_hash(docs)


def test_nondeterministic_nan_metadata_rejected():
    docs = [_doc("dbo.a", metadata={"bad": float("nan")})]
    with pytest.raises(ValueError):
        compute_document_set_hash(docs)


def test_infinite_metadata_value_rejected():
    docs = [_doc("dbo.a", metadata={"bad": float("inf")})]
    with pytest.raises(ValueError):
        compute_document_set_hash(docs)


def test_custom_object_metadata_value_rejected_not_reprd():
    class _Weird:
        def __repr__(self):
            return "WEIRD"

    docs = [_doc("dbo.a", metadata={"bad": _Weird()})]
    with pytest.raises(ValueError):
        compute_document_set_hash(docs)


def test_old_v1_style_build_is_not_reused_after_upgrading_the_hash_format(db_path):
    """Simulates a build persisted before this step (old 2-field hash, no
    metadata, no version separator) — proves the NEW compute_document_set_
    hash() naturally never matches it for the same logical documents, so
    persist_retrieval_build() creates a new build rather than reusing the
    stale one. No migration, no stored version column — the hash VALUE
    itself is the version signal."""
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.a", metadata={"business_name": "Alpha"})]
    fingerprint = _fingerprint()
    now = "2026-09-08T00:00:00Z"

    old_hash = _old_v1_document_set_hash(docs)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (500, 1, 1, 1, ?, ?, ?, ?, 'READY', 1, ?, ?)",
        (
            fingerprint["fingerprint_version"], fingerprint["fingerprint_hash"],
            fingerprint["table_set_hash"], old_hash, now, now,
        ),
    )
    conn.commit()
    conn.close()

    result = persist_retrieval_build(_batch(1, 1, 1, docs), fingerprint, "alice")

    assert result.outcome == OUTCOME_CREATED  # NOT reused — old-format hash never matches
    assert result.build.id != 500
    assert result.build.document_set_hash != old_hash

    # The pre-existing (simulated old-version) row is completely untouched.
    conn = sqlite3.connect(db_path)
    old_row = conn.execute(
        "SELECT status, document_set_hash FROM sql_retrieval_builds WHERE id = 500"
    ).fetchone()
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert old_row == ("READY", old_hash)
    assert build_count == 2  # the simulated old build AND the new one, both present


def test_new_version_preparation_creates_one_new_build_then_reuses_it(db_path):
    """The 'new version-2 preparation creates one new build' + 'unchanged
    version-2 preparation reuses it' pair, exercised back to back against
    real persist_retrieval_build() calls (not compute_document_set_hash in
    isolation)."""
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    docs = [_doc("dbo.a", metadata={"business_name": "Alpha", "synonyms": ("alpha",)})]
    fingerprint = _fingerprint()

    first = persist_retrieval_build(_batch(1, 1, 1, docs), fingerprint, "alice")
    assert first.outcome == OUTCOME_CREATED

    second = persist_retrieval_build(_batch(1, 1, 1, docs), fingerprint, "alice")
    assert second.outcome == OUTCOME_REUSED
    assert second.build.id == first.build.id

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1


def test_metadata_only_change_creates_new_build_reusing_document_hashes(db_path):
    """embeddings are fully reused when document hashes are unchanged —
    proven at the unit level by showing a metadata-only-driven new build's
    documents carry IDENTICAL document_hash values to the prior build's
    (the exact key core/sqlservice/embedding_store.py caches embeddings
    under, independent of build_id) — historical READY build also proven
    untouched here."""
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    fingerprint = _fingerprint()

    docs_v1 = [_doc("dbo.a", text="same text forever")]  # no business_name yet
    first = persist_retrieval_build(_batch(1, 1, 1, docs_v1), fingerprint, "alice")
    assert first.outcome == OUTCOME_CREATED
    first_stored = load_stored_retrieval_documents(first.build.id, 1, "alice")[0]

    docs_v2 = [_doc("dbo.a", text="same text forever", metadata={"business_name": "Alpha"})]
    second = persist_retrieval_build(_batch(1, 1, 1, docs_v2), fingerprint, "alice")
    assert second.outcome == OUTCOME_CREATED  # metadata differs -> new build, not reused
    assert second.build.id != first.build.id
    second_stored = load_stored_retrieval_documents(second.build.id, 1, "alice")[0]

    # Same document_hash across both builds (document_text never changed) —
    # this is exactly the cache key embedding_store.py reuses vectors by.
    assert first_stored.document_hash == second_stored.document_hash

    # The historical (first) build's row is byte-identical after the second
    # build was created — never mutated in place.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, document_set_hash, document_count FROM sql_retrieval_builds WHERE id = ?",
        (first.build.id,),
    ).fetchone()
    conn.close()
    assert row == ("READY", first.build.document_set_hash, 1)


def test_concurrency_and_idempotency_protections_remain_intact_with_versioned_hash(db_path, monkeypatch):
    """Re-exercises the existing race-simulation scenario with metadata-
    bearing documents, proving the Step 5A concurrency-conflict-recovery
    path is unaffected by the Step 4B hash-payload change."""
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, 1, source_id=1)
    fingerprint = _fingerprint()
    docs = [_doc("dbo.a", metadata={"business_name": "Alpha", "synonyms": ("alpha",)})]
    document_set_hash = compute_document_set_hash(docs)
    now = "2026-09-08T00:00:00Z"

    conflicting_sql = (
        "INSERT INTO sql_retrieval_builds "
        "(source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (1, 1, 1, ?, ?, ?, ?, 'READY', 1, ?, ?)"
    )
    conflicting_params = (
        fingerprint["fingerprint_version"], fingerprint["fingerprint_hash"],
        fingerprint["table_set_hash"], document_set_hash, now, now,
    )

    def _fake_get_connection():
        real = _make_connection(db_path)
        return _RaceSimulatingConnection(real, db_path, conflicting_sql, conflicting_params)

    monkeypatch.setattr(retrieval_store_module, "get_connection", _fake_get_connection)

    result = persist_retrieval_build(_batch(1, 1, 1, docs), fingerprint, "alice")

    assert result.outcome == OUTCOME_REUSED
    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1
