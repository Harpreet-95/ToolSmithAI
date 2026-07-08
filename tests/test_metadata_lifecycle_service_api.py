"""
Tests for the data-layer functions backing the Phase 11 metadata-lifecycle API
routes (api/v1/lifecycle_routes.py): trigger_manual_lifecycle_run,
list_lifecycle_runs, get_lifecycle_run — ownership checks, run history, and
manual-trigger behavior.

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file.

Run from the project root:
    python -m pytest tests/test_metadata_lifecycle_service_api.py -v
"""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-lifecycle-api-long-enough!!!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-lifecycle-api-value")

import data.models as models
from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from data.lifecycle_service import (
    get_lifecycle_run,
    list_lifecycle_runs,
    trigger_manual_lifecycle_run,
)

_NOW = "2026-01-01T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.lifecycle_service",
    "data.review_task_service",
    "data.dictionary_service",
    "data.domain_service",
    "data.entity_service",
    "data.notification_service",
    "data.audit",
    "data.governance_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lifecycle_api.db")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _insert_schema_and_profiling_snapshot(db_path: str, version: int) -> None:
    snap = SchemaSnapshot(
        source_id=1, source_type="mssql", discovered_at=_NOW,
        schemas=[SchemaInfo(schema_name="dbo", tables=[TableInfo(
            table_name="customers", schema_name="dbo", table_fqn="dbo.customers",
            table_type="TABLE",
            columns=[ColumnInfo(
                column_name="id", ordinal_position=1, data_type="INTEGER",
                raw_type="int", is_nullable=False, is_primary_key=True, is_identity=True,
            )],
        )])],
    )
    conn = _db_conn(db_path)
    cursor = conn.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, column_count, "
        " snapshot_json, discovered_at, created_at) VALUES (1, ?, 'mssql', 1, 0, 1, ?, ?, ?)",
        (version, json.dumps(dataclasses.asdict(snap)), _NOW, _NOW),
    )
    schema_id = cursor.lastrowid
    prof_cursor = conn.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, created_at) VALUES (1, ?, ?, ?)",
        (schema_id, version, _NOW),
    )
    prof_id = prof_cursor.lastrowid
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, created_at, updated_at) "
        "VALUES (?, 1, 'dbo.customers', 'customers', 'dbo', ?, ?)",
        (prof_id, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


class TestTriggerManualLifecycleRun:
    def test_ownership_check_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert trigger_manual_lifecycle_run(1, "not-the-owner") is None

    def test_ownership_check_missing_source_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert trigger_manual_lifecycle_run(999, "u1") is None

    def test_successful_manual_trigger_returns_serialized_result(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _insert_schema_and_profiling_snapshot(db, 1)
        result = trigger_manual_lifecycle_run(1, "u1")
        assert result is not None
        assert result["status"] == "COMPLETE"
        assert result["trigger"] == "manual"
        assert result["source_id"] == 1
        assert result["run_id"] is not None


class TestListAndGetLifecycleRuns:
    def test_list_returns_none_for_wrong_user(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert list_lifecycle_runs(1, "not-the-owner") is None

    def test_list_empty_before_any_run(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert list_lifecycle_runs(1, "u1") == []

    def test_list_returns_newest_first(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _insert_schema_and_profiling_snapshot(db, 1)
        trigger_manual_lifecycle_run(1, "u1")
        _insert_schema_and_profiling_snapshot(db, 2)
        trigger_manual_lifecycle_run(1, "u1")

        runs = list_lifecycle_runs(1, "u1")
        assert len(runs) == 2
        assert runs[0]["id"] > runs[1]["id"]

    def test_get_returns_none_for_wrong_user(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _insert_schema_and_profiling_snapshot(db, 1)
        result = trigger_manual_lifecycle_run(1, "u1")
        assert get_lifecycle_run(1, "not-the-owner", result["run_id"]) is None

    def test_get_returns_none_for_missing_run(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert get_lifecycle_run(1, "u1", 9999) is None

    def test_get_returns_run_with_parsed_steps(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        _insert_schema_and_profiling_snapshot(db, 1)
        result = trigger_manual_lifecycle_run(1, "u1")
        run = get_lifecycle_run(1, "u1", result["run_id"])
        assert run is not None
        assert isinstance(run["steps_executed"], list)
        assert len(run["steps_executed"]) == 10
