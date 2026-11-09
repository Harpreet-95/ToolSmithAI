"""
Autonomous connect->bootstrap trigger (Autonomous Live-SQL Foundation,
Part 2-3 & 18 — redefined from the original Part 5-6).

Before the original Part 5-6 change, create_data_source() queued a
metadata_jobs row but nothing ever drained it automatically — a user had to
separately click "Scan Metadata" (POST /metadata-jobs/{id}/run) to get
dictionary/domain/entity/PII/relationship metadata. create_data_source() now
fires a job on a background daemon thread right after creating the job,
gated by core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED (default True
in production, default False under pytest — see that flag's own docstring
for why: dozens of unrelated tests call create_data_source() as setup and
use per-test temp DBs, and an unawaited background thread surviving past
monkeypatch teardown caused cross-test contamination).

As of the "make question-driven on-demand intelligence the default"
milestone, the job that fires automatically is run_connect_bootstrap_job —
LIGHTWEIGHT (schema discovery + schema-only structural profiling + cheap
PII classification), never the old full-scan run_metadata_job — unless
core.config.AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED is also set,
which opts back into the legacy eager full pipeline. See
tests/test_connect_time_lightweight_bootstrap_fixture.py for the empirical
end-to-end proof of the lightweight job's scope and the on-demand
question-time self-healing path; this file only covers the TRIGGER
(create_data_source starts the right job function without being asked).

This file explicitly re-enables the flag to test the behavior it gates.

Run from the project root:
    python -m pytest tests/test_autonomous_connect_metadata.py -v
"""
from __future__ import annotations

import os
import sqlite3
import time

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-autonomous-connect-secret-long-enough-12345")
os.environ.setdefault("USER_ID_SALT", "test-autonomous-connect-salt-long-enough-98765")

import core.connectors.relational.mssql  # noqa: F401 — populates ConnectorRegistry
from core.connectors.relational.mssql import SQLServerConnector
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo

import data.models as models
import data.datasource_service as datasource_service
from data.datasource_service import create_data_source


def _fake_snapshot(source_id: int) -> SchemaSnapshot:
    table = TableInfo(
        table_name="orders", schema_name="dbo", table_fqn="dbo.orders", table_type="TABLE",
        columns=[ColumnInfo(column_name="OrderID", ordinal_position=1, data_type="INTEGER",
                             raw_type="int", is_nullable=False, is_primary_key=True, is_identity=True)],
    )
    return SchemaSnapshot(
        source_id=source_id, source_type="mssql", discovered_at="2026-08-22T00:00:00+00:00",
        schemas=[SchemaInfo(schema_name="dbo", tables=[table])],
    )

_USER = "user-1"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "autonomous_connect.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr(datasource_service, "get_connection", lambda p=db_path: _db_conn(p))
    # Re-enable the flag this file exists to test — off by default under
    # pytest (see module docstring).
    monkeypatch.setattr("core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED", True)
    return db_path


def _create_source_payload():
    return {
        "display_name": "Prod SQL Server",
        "source_type": "mssql",
        "config": {"host": "db.internal", "database": "EXAMPLECO", "auth_type": "sql",
                    "username": "svc", "password": "secret"},
        "metadata": {},
    }


def test_create_data_source_starts_background_bootstrap_job(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    # Fast fake — this test verifies the TRIGGER (create_data_source starts
    # the right job function without being asked) and that it picks the
    # LIGHTWEIGHT job by default, not run_connect_bootstrap_job's own
    # internals (covered by tests/test_connect_time_lightweight_bootstrap_fixture.py).
    calls = []
    full_calls = []

    def _fake_run_connect_bootstrap_job(job_id):
        conn = _db_conn(db_path)
        conn.execute(
            "UPDATE metadata_jobs SET status = 'COMPLETE', current_step = 'READY', "
            "progress_message = 'Ready to ask.' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        conn.close()
        calls.append(job_id)  # appended last — the poll loop below must only
        # observe this once the write has actually landed, not before.

    monkeypatch.setattr(datasource_service, "run_connect_bootstrap_job", _fake_run_connect_bootstrap_job)
    monkeypatch.setattr(datasource_service, "run_metadata_job", lambda job_id: full_calls.append(job_id))
    # Bootstrap steps (schema discovery, vocabulary) are best-effort and
    # independent of the trigger under test — let them fail silently
    # (no live connector wired) rather than mocking them out.

    record = create_data_source(_USER, _create_source_payload())
    job_id = record["metadata_job_id"]
    assert job_id is not None

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not calls:
        time.sleep(0.02)

    assert calls == [job_id]
    assert full_calls == []  # the legacy full pipeline must never fire by default

    conn = _db_conn(db_path)
    row = conn.execute("SELECT status FROM metadata_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    assert row["status"] == "COMPLETE"


def test_flag_disabled_does_not_start_background_job(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.AUTONOMOUS_METADATA_ON_CONNECT_ENABLED", False)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    calls = []
    monkeypatch.setattr(datasource_service, "run_connect_bootstrap_job", lambda job_id: calls.append(job_id))
    monkeypatch.setattr(datasource_service, "run_metadata_job", lambda job_id: calls.append(job_id))

    record = create_data_source(_USER, _create_source_payload())
    job_id = record["metadata_job_id"]

    time.sleep(0.2)  # give a wrongly-spawned thread a chance to fire
    assert calls == []

    conn = _db_conn(db_path)
    row = conn.execute("SELECT status FROM metadata_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    assert row["status"] == "QUEUED"  # untouched — exactly the pre-Part-5 behavior


def test_full_enrichment_flag_runs_legacy_pipeline_on_connect(tmp_path, monkeypatch):
    """Part 15/18 — AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED is the
    explicit opt-in back into the old eager full-scan-on-connect behavior.
    When set, the background thread must call run_metadata_job (full
    pipeline), never run_connect_bootstrap_job (lightweight)."""
    db_path = _env(tmp_path, monkeypatch)
    monkeypatch.setattr("core.config.AUTONOMOUS_FULL_ENRICHMENT_ON_CONNECT_ENABLED", True)
    monkeypatch.setattr(
        SQLServerConnector, "discover_schema",
        lambda self, config: _fake_snapshot(config.params["_source_id"]),
    )

    light_calls = []
    full_calls = []

    def _fake_run_metadata_job(job_id):
        conn = _db_conn(db_path)
        conn.execute(
            "UPDATE metadata_jobs SET status = 'COMPLETE', current_step = 'READY', "
            "progress_message = 'Metadata ready.' WHERE id = ?",
            (job_id,),
        )
        conn.commit()
        conn.close()
        full_calls.append(job_id)

    monkeypatch.setattr(datasource_service, "run_connect_bootstrap_job", lambda job_id: light_calls.append(job_id))
    monkeypatch.setattr(datasource_service, "run_metadata_job", _fake_run_metadata_job)

    record = create_data_source(_USER, _create_source_payload())
    job_id = record["metadata_job_id"]
    assert job_id is not None

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not full_calls:
        time.sleep(0.02)

    assert full_calls == [job_id]
    assert light_calls == []
