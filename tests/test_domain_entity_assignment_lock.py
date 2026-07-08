"""
Tests for the domain/entity assignment human-lock mechanism added in Phase 11
(assignment_source column + lock_domain_assignment / lock_entity_assignment).

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file.

Run from the project root:
    python -m pytest tests/test_domain_entity_assignment_lock.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-assignment-lock-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-assignment-lock-value")

import data.models as models
from data.domain_service import generate_domain_assignments, lock_domain_assignment
from data.entity_service import generate_entity_assignments, lock_entity_assignment

_NOW = "2026-01-01T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.domain_service",
    "data.entity_service",
    "data.governance_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "lock.db")

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
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, 1, 1, 'mssql', '{}', ?, ?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1, 1, 1, 1, ?)",
        (_NOW,),
    )
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, created_at, updated_at) "
        "VALUES (1, 1, 'dbo.customers', 'customers', 'dbo', ?, ?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _domain_row(db_path):
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM domain_assignments WHERE source_id = 1 AND table_fqn = 'dbo.customers'"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _entity_row(db_path):
    conn = _db_conn(db_path)
    row = conn.execute(
        "SELECT * FROM entity_assignments WHERE source_id = 1 AND table_fqn = 'dbo.customers'"
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _governance_events(db_path, object_type_id):
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM governance_approval_events WHERE object_type_id = ?", (object_type_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class TestDomainAssignmentLock:
    def test_generate_sets_assignment_source_rule_by_default(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        row = _domain_row(db)
        assert row["assignment_source"] == "rule"

    def test_lock_sets_human_and_optional_new_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        result = lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        assert result["assignment_source"] == "human"
        assert result["domain"] == "Custom Domain"

    def test_lock_without_domain_keeps_existing_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        before = _domain_row(db)
        lock_domain_assignment(1, "u1", "dbo.customers")
        after = _domain_row(db)
        assert after["domain"] == before["domain"]
        assert after["assignment_source"] == "human"

    def test_locked_row_not_overwritten_by_regeneration(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        generate_domain_assignments(1, "u1")  # re-run — must not overwrite
        row = _domain_row(db)
        assert row["domain"] == "Custom Domain"
        assert row["assignment_source"] == "human"

    def test_lock_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_domain_assignment(1, "u1", "dbo.nonexistent") is None

    def test_lock_wrong_user_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_domain_assignment(1, "someone-else", "dbo.customers") is None

    def test_lock_logs_governance_event(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_domain_assignments(1, "u1")
        lock_domain_assignment(1, "u1", "dbo.customers", domain="Custom Domain")
        events = _governance_events(db, "domain.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "HUMAN_LOCK"
        assert events[0]["to_state"] == "HUMAN_APPROVED"


class TestEntityAssignmentLock:
    def test_generate_sets_assignment_source_rule_by_default(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        row = _entity_row(db)
        assert row["assignment_source"] == "rule"

    def test_lock_sets_human_and_optional_new_value(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        result = lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        assert result["assignment_source"] == "human"
        assert result["entity"] == "Custom Entity"

    def test_locked_row_not_overwritten_by_regeneration(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        generate_entity_assignments(1, "u1")
        row = _entity_row(db)
        assert row["entity"] == "Custom Entity"
        assert row["assignment_source"] == "human"

    def test_lock_nonexistent_assignment_returns_none(self, tmp_path, monkeypatch):
        env(tmp_path, monkeypatch)
        assert lock_entity_assignment(1, "u1", "dbo.nonexistent") is None

    def test_lock_logs_governance_event(self, tmp_path, monkeypatch):
        db = env(tmp_path, monkeypatch)
        generate_entity_assignments(1, "u1")
        lock_entity_assignment(1, "u1", "dbo.customers", entity="Custom Entity")
        events = _governance_events(db, "entity.assignment")
        assert len(events) == 1
        assert events[0]["event_type"] == "HUMAN_LOCK"
