"""
Regression tests for data.datasource_service.delete_data_source() — added
after investigating a "Remove does nothing" report on the Data Sources page.

No test previously covered this function at all. The behavior confirmed
live against a running server during that investigation:
  - a source with an in-flight metadata_jobs row (QUEUED/RUNNING) is
    correctly refused (ValueError -> 409 at the route layer) rather than
    deleted out from under a running scan;
  - once that job finishes, delete succeeds and ON DELETE CASCADE removes
    every dependent row (schema_snapshots, metadata_jobs, ...) with no
    orphans left behind;
  - a source belonging to a different user, or a nonexistent id, returns
    None (-> 404 at the route layer) rather than deleting someone else's row.

Run from the project root:
    venv/Scripts/pytest tests/test_delete_data_source.py -v
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-delete-source-secret-long-enough-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-delete-source-salt-long-enough-0987654321")

import data.models as models
from data.datasource_service import delete_data_source

_USER = "user-1"
_OTHER_USER = "user-2"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "delete_source.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.datasource_service.get_connection", lambda p=db_path: _db_conn(p))
    return db_path


def _insert_source(db_path: str, user_id: str = _USER) -> int:
    conn = _db_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO data_source_connections
                (user_id, display_name, source_type, source_category,
                 encrypted_config_json, created_at, updated_at)
            VALUES (?, 'Test Source', 'mssql', 'relational_db', '{}',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (user_id,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_job(db_path: str, source_id: int, status: str) -> int:
    conn = _db_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO metadata_jobs (source_id, user_id, status, created_at, updated_at)
            VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (source_id, _USER, status),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _insert_snapshot(db_path: str, source_id: int) -> int:
    conn = _db_conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO schema_snapshots (source_id, source_type, discovered_at, created_at, snapshot_json)
            VALUES (?, 'mssql', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '{}')
            """,
            (source_id,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_delete_succeeds_and_cascades_dependent_rows(env):
    source_id = _insert_source(env)
    _insert_snapshot(env, source_id)

    result = delete_data_source(source_id, _USER)

    assert result == {"id": source_id, "display_name": "Test Source", "deleted": True}
    conn = _db_conn(env)
    try:
        assert conn.execute(
            "SELECT 1 FROM data_source_connections WHERE id = ?", (source_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM schema_snapshots WHERE source_id = ?", (source_id,)
        ).fetchone() is None
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["QUEUED", "RUNNING"])
def test_delete_blocked_while_metadata_job_in_flight(env, status):
    source_id = _insert_source(env)
    _insert_job(env, source_id, status)

    with pytest.raises(ValueError, match="Metadata job is still running"):
        delete_data_source(source_id, _USER)

    conn = _db_conn(env)
    try:
        assert conn.execute(
            "SELECT 1 FROM data_source_connections WHERE id = ?", (source_id,)
        ).fetchone() is not None
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["COMPLETE", "FAILED", "CANCELLED"])
def test_delete_allowed_once_job_no_longer_in_flight(env, status):
    source_id = _insert_source(env)
    _insert_job(env, source_id, status)

    result = delete_data_source(source_id, _USER)

    assert result is not None
    assert result["deleted"] is True


def test_delete_returns_none_for_nonexistent_source(env):
    assert delete_data_source(999999, _USER) is None


def test_delete_returns_none_for_wrong_owner(env):
    source_id = _insert_source(env, user_id=_OTHER_USER)

    assert delete_data_source(source_id, _USER) is None

    conn = _db_conn(env)
    try:
        assert conn.execute(
            "SELECT 1 FROM data_source_connections WHERE id = ?", (source_id,)
        ).fetchone() is not None
    finally:
        conn.close()
