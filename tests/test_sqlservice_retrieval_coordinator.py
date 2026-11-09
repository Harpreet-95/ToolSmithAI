"""
Focused unit tests — SQL rebuild Phase 1, Step 6: connection-time
retrieval-build coordinator (core/sqlservice/retrieval_coordinator.py).

Uses temporary on-disk SQLite databases only — never the real
data/toolsmith.db, never SQL Server, never OpenAI. Schema is created via
the real `data.models.init_db()` (monkeypatched to a temp file, then
copied per test for speed) so these tests exercise the actual production
schema. `get_connection` is monkeypatched in every module that opens one
(data.models, core.sqlservice.metadata_loader, core.sqlservice.
retrieval_store, data.schema_service) so the whole coordinator chain runs
against the temp file end to end.

Run:
    venv/Scripts/pytest tests/test_sqlservice_retrieval_coordinator.py -v
"""

import dataclasses
import json
import os
import shutil
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

import data.models as models_module
import data.schema_service as schema_service_module
import core.sqlservice.metadata_loader as metadata_loader_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.retrieval_coordinator as retrieval_coordinator_module
from core.connectors.schema import ColumnInfo, PrimaryKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.sqlservice.retrieval_coordinator import (
    STATUS_CREATED,
    STATUS_FINGERPRINT_FAILED,
    STATUS_METADATA_LOAD_FAILED,
    STATUS_PERSISTENCE_FAILED,
    STATUS_REUSED,
    STATUS_SNAPSHOT_MISMATCH,
    build_and_persist_retrieval_documents,
)


# ── Fixture plumbing ──────────────────────────────────────────────────────


def _make_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    """One real init_db() per module; every test gets a fast file copy."""
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


def _col(name, data_type, *, pk=False, ordinal=1):
    return ColumnInfo(
        column_name=name, ordinal_position=ordinal, data_type=data_type,
        raw_type=data_type.lower(), is_nullable=True, is_primary_key=pk, is_identity=pk,
    )


def _table(schema_name, table_name, columns):
    return TableInfo(
        table_name=table_name, schema_name=schema_name, table_fqn=f"{schema_name}.{table_name}",
        table_type="TABLE", columns=columns,
        primary_keys=[PrimaryKeyInfo(c.column_name, 1) for c in columns if c.is_primary_key],
        foreign_keys=[],
    )


def _snapshot_json(tables, source_id=1, discovered_at="2026-09-04T00:00:00Z"):
    snapshot = SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at=discovered_at,
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )
    return json.dumps(dataclasses.asdict(snapshot))


def _insert_snapshot(db_path, snapshot_id, source_id, version, tables=None):
    if tables is None:
        tables = [_table("dbo", "learners", [_col("id", "INTEGER", pk=True)])]
    conn = sqlite3.connect(db_path)
    now = "2026-09-04T00:00:00Z"
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, ?, 'mssql', ?, ?, ?)",
        (snapshot_id, source_id, version, _snapshot_json(tables, source_id=source_id), now, now),
    )
    conn.commit()
    conn.close()


def _default_setup(db_path, source_id=1, user_id="alice"):
    _insert_source(db_path, source_id, user_id)
    _insert_snapshot(db_path, snapshot_id=1, source_id=source_id, version=1)


# ── Successful new build / reuse ───────────────────────────────────────────

def test_successful_new_build(db_path):
    _default_setup(db_path)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_CREATED
    assert result.persist_outcome == "CREATED"
    assert result.build_id is not None
    assert result.document_count == 1
    assert result.snapshot_id == 1
    assert result.snapshot_version == 1
    assert result.fingerprint_version is not None
    assert result.schema_fingerprint is not None
    assert result.attempts == 1
    assert result.error is None


def test_reuse_of_unchanged_build(db_path):
    _default_setup(db_path)

    first = build_and_persist_retrieval_documents(1, "alice")
    second = build_and_persist_retrieval_documents(1, "alice")

    assert first.status == STATUS_CREATED
    assert second.status == STATUS_REUSED
    assert second.persist_outcome == "REUSED"
    assert second.build_id == first.build_id
    assert second.attempts == 1

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1


def test_deterministic_typed_results(db_path):
    """Same input state -> same typed outcome, repeatedly."""
    _default_setup(db_path)

    first = build_and_persist_retrieval_documents(1, "alice")
    second = build_and_persist_retrieval_documents(1, "alice")
    third = build_and_persist_retrieval_documents(1, "alice")

    assert first.status == STATUS_CREATED
    assert second.status == STATUS_REUSED
    assert third.status == STATUS_REUSED
    for result in (first, second, third):
        assert result.status in {
            STATUS_CREATED, STATUS_REUSED, STATUS_METADATA_LOAD_FAILED,
            STATUS_SNAPSHOT_MISMATCH, STATUS_FINGERPRINT_FAILED, STATUS_PERSISTENCE_FAILED,
        }


# ── Metadata-loader failure propagation ────────────────────────────────────

def test_metadata_loader_failure_propagates(db_path):
    _insert_source(db_path, 1)
    # No snapshot inserted -> loader status NO_SNAPSHOT.

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_METADATA_LOAD_FAILED
    assert result.attempts == 1
    assert "NO_SNAPSHOT" in result.error
    assert result.build_id is None

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds").fetchone()[0]
    conn.close()
    assert build_count == 0


def test_unowned_source_is_metadata_load_failure(db_path):
    _default_setup(db_path, user_id="alice")

    result = build_and_persist_retrieval_documents(1, "mallory")

    assert result.status == STATUS_METADATA_LOAD_FAILED
    assert "UNOWNED" in result.error


# ── Snapshot race: one mismatch then successful retry ─────────────────────

def test_one_snapshot_race_then_successful_retry(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1)

    real_get_latest_snapshot = schema_service_module.get_latest_snapshot
    call_count = {"n": 0}

    def _racing_get_latest_snapshot(source_id, user_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate a concurrent discovery run landing a new snapshot
            # right as we start verifying the fingerprint for the one the
            # already-loaded batch used.
            _insert_snapshot(db_path, snapshot_id=2, source_id=1, version=2)
        return real_get_latest_snapshot(source_id, user_id)

    monkeypatch.setattr(retrieval_coordinator_module, "get_latest_snapshot", _racing_get_latest_snapshot)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_CREATED
    assert result.attempts == 2
    assert result.snapshot_id == 2
    assert result.snapshot_version == 2

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds WHERE source_id = 1").fetchone()[0]
    conn.close()
    assert build_count == 1  # only one build, for the final consistent (batch, fingerprint) pair


def test_repeated_mismatch_exhausts_retry_limit(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1)

    real_get_latest_snapshot = schema_service_module.get_latest_snapshot
    next_id = {"n": 2}

    def _always_racing_get_latest_snapshot(source_id, user_id):
        version = next_id["n"]
        next_id["n"] += 1
        _insert_snapshot(db_path, snapshot_id=version, source_id=1, version=version)
        return real_get_latest_snapshot(source_id, user_id)

    monkeypatch.setattr(retrieval_coordinator_module, "get_latest_snapshot", _always_racing_get_latest_snapshot)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_SNAPSHOT_MISMATCH
    assert result.attempts == 3

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds").fetchone()[0]
    conn.close()
    assert build_count == 0  # never persisted a mismatched batch/fingerprint pair


# ── Fingerprint / persistence exceptions ───────────────────────────────────

def test_fingerprint_exception_is_reported_and_does_not_persist(db_path, monkeypatch):
    _default_setup(db_path)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated fingerprint failure")

    monkeypatch.setattr(retrieval_coordinator_module, "compute_fingerprint", _raise)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_FINGERPRINT_FAILED
    assert "simulated fingerprint failure" in result.error
    assert result.build_id is None

    conn = sqlite3.connect(db_path)
    build_count = conn.execute("SELECT COUNT(*) FROM sql_retrieval_builds").fetchone()[0]
    conn.close()
    assert build_count == 0


def test_persistence_exception_is_reported(db_path, monkeypatch):
    _default_setup(db_path)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated persistence failure")

    monkeypatch.setattr(retrieval_coordinator_module, "persist_retrieval_build", _raise)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_PERSISTENCE_FAILED
    assert "simulated persistence failure" in result.error
    assert result.build_id is None
    assert result.fingerprint_version is not None  # fingerprinting itself succeeded


# ── No persistence call on validation/consistency failure ─────────────────

def test_no_persistence_call_when_metadata_load_fails(db_path, monkeypatch):
    _insert_source(db_path, 1)  # no snapshot

    calls = []
    real_persist = retrieval_coordinator_module.persist_retrieval_build

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(retrieval_coordinator_module, "persist_retrieval_build", _spy)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_METADATA_LOAD_FAILED
    assert calls == []


def test_no_persistence_call_when_snapshot_never_stabilizes(db_path, monkeypatch):
    _insert_source(db_path, 1)
    _insert_snapshot(db_path, snapshot_id=1, source_id=1, version=1)

    real_get_latest_snapshot = schema_service_module.get_latest_snapshot
    next_id = {"n": 2}

    def _always_racing_get_latest_snapshot(source_id, user_id):
        version = next_id["n"]
        next_id["n"] += 1
        _insert_snapshot(db_path, snapshot_id=version, source_id=1, version=version)
        return real_get_latest_snapshot(source_id, user_id)

    monkeypatch.setattr(retrieval_coordinator_module, "get_latest_snapshot", _always_racing_get_latest_snapshot)

    calls = []
    real_persist = retrieval_coordinator_module.persist_retrieval_build

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(retrieval_coordinator_module, "persist_retrieval_build", _spy)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_SNAPSHOT_MISMATCH
    assert calls == []


# ── Correct arguments passed to persistence ────────────────────────────────

def test_correct_source_snapshot_version_fingerprint_passed_to_persistence(db_path, monkeypatch):
    _default_setup(db_path)

    captured = {}
    real_persist = retrieval_coordinator_module.persist_retrieval_build

    def _spy(batch, fingerprint, user_id):
        captured["batch"] = batch
        captured["fingerprint"] = fingerprint
        captured["user_id"] = user_id
        return real_persist(batch, fingerprint, user_id)

    monkeypatch.setattr(retrieval_coordinator_module, "persist_retrieval_build", _spy)

    result = build_and_persist_retrieval_documents(1, "alice")

    assert result.status == STATUS_CREATED
    assert captured["batch"].source_id == 1
    assert captured["batch"].snapshot_id == 1
    assert captured["batch"].snapshot_version == 1
    assert captured["fingerprint"]["fingerprint_version"] >= 1
    assert captured["fingerprint"]["fingerprint_hash"] == result.schema_fingerprint
    assert captured["user_id"] == "alice"
