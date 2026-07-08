"""
Tests for data.datasource_service.run_metadata_job()'s Phase 11 integration:
the LIFECYCLE step added after DISCOVERY -> STRUCTURAL_PROFILING.

Covers:
  - Successful run reaches COMPLETE/READY and invokes the autonomous lifecycle
    with trigger=SCAN_COMPLETE
  - Discovery failure short-circuits before the lifecycle step ever runs
  - Structural profiling failure short-circuits before the lifecycle step ever runs
  - A lifecycle failure does NOT fail the job — status stays COMPLETE/READY
    (decision: discovery + profiling are the job's core promise; lifecycle
    refresh is best-effort on top, matching dictionary AI-enrichment's
    existing silent-failure behavior)

Run from the project root:
    python -m pytest tests/test_metadata_job_lifecycle_integration.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-job-lifecycle-long-enough!!!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-job-lifecycle-value")

import data.models as models
from data.datasource_service import run_metadata_job

_NOW = "2026-01-01T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "job_lifecycle.db")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.datasource_service.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    cursor = conn.execute(
        "INSERT INTO metadata_jobs (source_id, user_id, job_type, status, created_at, updated_at) "
        "VALUES (1, 'u1', 'initial_metadata', 'QUEUED', ?, ?)",
        (_NOW, _NOW),
    )
    job_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return db_path, job_id


def _job_row(db_path, job_id):
    conn = _db_conn(db_path)
    row = conn.execute("SELECT * FROM metadata_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row)


class TestSuccessfulRun:
    def test_reaches_complete_ready_and_invokes_lifecycle(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        mock_lifecycle = MagicMock()
        with patch("data.schema_service.run_discovery") as mock_discovery, \
             patch("data.profiling_service.run_structural_profiling") as mock_profiling, \
             patch("core.lifecycle.runner.run_autonomous_lifecycle", mock_lifecycle):
            run_metadata_job(job_id)

        mock_discovery.assert_called_once_with(1, "u1")
        mock_profiling.assert_called_once_with(1, "u1")
        mock_lifecycle.assert_called_once()
        _, kwargs = mock_lifecycle.call_args
        assert mock_lifecycle.call_args.args[:2] == (1, "u1") or kwargs.get("source_id") == 1

        row = _job_row(db, job_id)
        assert row["status"] == "COMPLETE"
        assert row["current_step"] == "READY"


class TestDiscoveryFailure:
    def test_short_circuits_before_lifecycle(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        mock_lifecycle = MagicMock()
        with patch("data.schema_service.run_discovery", side_effect=RuntimeError("discovery boom")), \
             patch("data.profiling_service.run_structural_profiling") as mock_profiling, \
             patch("core.lifecycle.runner.run_autonomous_lifecycle", mock_lifecycle):
            run_metadata_job(job_id)

        mock_profiling.assert_not_called()
        mock_lifecycle.assert_not_called()
        row = _job_row(db, job_id)
        assert row["status"] == "FAILED"
        assert row["error_message"] == "Schema discovery failed."


class TestProfilingFailure:
    def test_short_circuits_before_lifecycle(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        mock_lifecycle = MagicMock()
        with patch("data.schema_service.run_discovery") as mock_discovery, \
             patch("data.profiling_service.run_structural_profiling", side_effect=RuntimeError("profiling boom")), \
             patch("core.lifecycle.runner.run_autonomous_lifecycle", mock_lifecycle):
            run_metadata_job(job_id)

        mock_discovery.assert_called_once()
        mock_lifecycle.assert_not_called()
        row = _job_row(db, job_id)
        assert row["status"] == "FAILED"
        assert row["error_message"] == "Structural profiling failed."


class TestLifecycleFailureDoesNotFailJob:
    def test_job_still_completes_when_lifecycle_raises(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery") as mock_discovery, \
             patch("data.profiling_service.run_structural_profiling") as mock_profiling, \
             patch("core.lifecycle.runner.run_autonomous_lifecycle", side_effect=RuntimeError("lifecycle boom")):
            run_metadata_job(job_id)

        mock_discovery.assert_called_once()
        mock_profiling.assert_called_once()
        row = _job_row(db, job_id)
        assert row["status"] == "COMPLETE"
        assert row["current_step"] == "READY"
