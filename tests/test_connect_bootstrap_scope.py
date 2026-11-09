"""
Autonomous Live-SQL Foundation milestone ("make question-driven on-demand
intelligence the default") — unit-level coverage for the connect-time scope
narrowing itself, independent of the full end-to-end fixture in
tests/test_connect_time_lightweight_bootstrap_fixture.py.

Covers:
  - run_connect_bootstrap_job runs ONLY discovery + structural profiling +
    PII classification, and never start_batch_profiling(FULL) or
    run_autonomous_lifecycle (Part 24 items 1-4).
  - run_metadata_job (legacy/manual "Scan Metadata") still runs the full
    pipeline unchanged (Part 24 item 15).
  - is_ready_to_ask reflects only ACTIVE + a schema_snapshots row, never
    profiling/dictionary/domain/entity/relationship/contract completeness
    (Part 24 item 5).
  - ODBC/live-connection timeout threading: _load_source_connection passes
    timeout_s through to the connector, and relationship_service's
    _open_live_connection requests a short bounded timeout rather than the
    connector's normal default (Part 24 items 19-20).

Run from the project root:
    python -m pytest tests/test_connect_bootstrap_scope.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-bootstrap-scope-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-bootstrap-scope-value")

import data.models as models
from data.datasource_service import is_ready_to_ask, run_connect_bootstrap_job, run_metadata_job

_NOW = "2026-01-01T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch, *, with_snapshot=False):
    db_path = str(tmp_path / "bootstrap_scope.db")

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
    if with_snapshot:
        conn.execute(
            "INSERT INTO schema_snapshots "
            "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
            " discovered_at, created_at) VALUES (1,1,1,'mssql',1,'{}',?,?)",
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


class TestRunConnectBootstrapJobScope:
    def test_runs_discovery_structural_profiling_and_pii_classification_only(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery") as mock_discovery, \
             patch("data.profiling_service.run_structural_profiling") as mock_structural, \
             patch("data.pii_classification_service.run_pii_classification") as mock_pii, \
             patch("data.profiling_service.start_batch_profiling") as mock_batch_full, \
             patch("core.lifecycle.runner.run_autonomous_lifecycle") as mock_lifecycle:
            run_connect_bootstrap_job(job_id)

        mock_discovery.assert_called_once_with(1, "u1")
        mock_structural.assert_called_once_with(1, "u1")
        mock_pii.assert_called_once_with(1)

        # The whole point of this milestone: no full-scan work on connect.
        mock_batch_full.assert_not_called()
        mock_lifecycle.assert_not_called()

        row = _job_row(db, job_id)
        assert row["status"] == "COMPLETE"
        assert row["current_step"] == "READY"

    def test_discovery_failure_short_circuits_before_structural_profiling(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery", side_effect=RuntimeError("boom")), \
             patch("data.profiling_service.run_structural_profiling") as mock_structural, \
             patch("data.pii_classification_service.run_pii_classification") as mock_pii:
            run_connect_bootstrap_job(job_id)

        mock_structural.assert_not_called()
        mock_pii.assert_not_called()
        row = _job_row(db, job_id)
        assert row["status"] == "FAILED"

    def test_structural_profiling_failure_short_circuits_before_pii(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery"), \
             patch("data.profiling_service.run_structural_profiling", side_effect=RuntimeError("boom")), \
             patch("data.pii_classification_service.run_pii_classification") as mock_pii:
            run_connect_bootstrap_job(job_id)

        mock_pii.assert_not_called()
        row = _job_row(db, job_id)
        assert row["status"] == "FAILED"

    def test_pii_classification_failure_is_best_effort_job_still_completes(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery"), \
             patch("data.profiling_service.run_structural_profiling"), \
             patch("data.pii_classification_service.run_pii_classification", side_effect=RuntimeError("boom")):
            run_connect_bootstrap_job(job_id)

        row = _job_row(db, job_id)
        assert row["status"] == "COMPLETE"
        assert row["current_step"] == "READY"


class TestRunMetadataJobStillFull:
    def test_legacy_manual_job_still_runs_full_pipeline(self, tmp_path, monkeypatch):
        db, job_id = env(tmp_path, monkeypatch)
        with patch("data.schema_service.run_discovery") as mock_discovery, \
             patch("data.profiling_service.run_structural_profiling") as mock_structural, \
             patch("data.pii_classification_service.run_pii_classification") as mock_pii, \
             patch("data.profiling_service.start_batch_profiling") as mock_batch_full, \
             patch("core.lifecycle.runner.run_autonomous_lifecycle") as mock_lifecycle:
            run_metadata_job(job_id)

        mock_discovery.assert_called_once_with(1, "u1")
        mock_structural.assert_called_once_with(1, "u1")
        mock_pii.assert_called_once_with(1)
        mock_batch_full.assert_called_once()
        assert mock_batch_full.call_args.args[:2] == (1, "u1")
        assert mock_batch_full.call_args.kwargs.get("mode") == "FULL"
        mock_lifecycle.assert_called_once()

        row = _job_row(db, job_id)
        assert row["status"] == "COMPLETE"
        assert row["current_step"] == "READY"


class TestIsReadyToAsk:
    def test_false_without_schema_snapshot(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch, with_snapshot=False)
        assert is_ready_to_ask(1, "u1") is False

    def test_true_once_active_with_schema_snapshot(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch, with_snapshot=True)
        assert is_ready_to_ask(1, "u1") is True

    def test_false_for_wrong_user(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch, with_snapshot=True)
        assert is_ready_to_ask(1, "someone-else") is False

    def test_false_for_inactive_source(self, tmp_path, monkeypatch):
        db, _job_id = env(tmp_path, monkeypatch, with_snapshot=True)
        conn = _db_conn(db)
        conn.execute("UPDATE data_source_connections SET source_status = 'ERROR' WHERE id = 1")
        conn.commit()
        conn.close()
        assert is_ready_to_ask(1, "u1") is False

    def test_does_not_require_profiling_or_dictionary(self, tmp_path, monkeypatch):
        """The central Part 7 assertion: READY_TO_ASK must not silently start
        requiring profiling/dictionary rows just because they happen to be
        absent — it is genuinely defined by discovery alone."""
        env(tmp_path, monkeypatch, with_snapshot=True)
        # No profiling_column_profiles, data_dictionary_tables, domain, or
        # entity rows were inserted anywhere above, and is_ready_to_ask
        # never queries those tables — still True.
        assert is_ready_to_ask(1, "u1") is True


class TestLiveConnectionTimeoutThreading:
    def test_load_source_connection_passes_timeout_s_through(self, monkeypatch):
        import data.query_execution_service as qes

        captured = {}

        class _FakeConnector:
            def open_connection(self, config, timeout_s=None):
                captured["timeout_s"] = timeout_s
                return MagicMock()

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = {
            "source_type": "mssql", "encrypted_config_json": "ENC",
        }
        mock_conn.close = lambda: None
        monkeypatch.setattr(qes, "get_connection", lambda: mock_conn)
        monkeypatch.setattr(qes.get_secret_manager(), "decrypt_secret", lambda v: "{}")
        monkeypatch.setattr(qes, "_registry", {"mssql": _FakeConnector})

        db_conn, source_type = qes._load_source_connection(1, "u1", timeout_s=5)
        assert source_type == "mssql"
        assert captured["timeout_s"] == 5

        # Default call site (unchanged behavior) still passes None through.
        captured.clear()
        qes._load_source_connection(1, "u1")
        assert captured["timeout_s"] is None

    def test_relationship_service_requests_bounded_timeout_not_default(self, monkeypatch):
        import data.relationship_service as rel

        captured = {}

        def _fake_load_source_connection(source_id, user_id, timeout_s=None):
            captured["timeout_s"] = timeout_s
            raise RuntimeError("no live connector wired for this unit test")

        with patch("data.query_execution_service._load_source_connection", _fake_load_source_connection):
            conn, builder = rel._open_live_connection(1, "u1")

        assert conn is None and builder is None
        assert captured["timeout_s"] == rel._LIVE_VALIDATION_CONNECT_TIMEOUT_S
        assert captured["timeout_s"] < 30  # strictly less than the old blanket 30s default
