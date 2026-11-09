"""
Tests for data/audit.py — Phase B / Task 4.

Covers normal-conditions logging (DB row + flat-file line both written) and
the size-based rotation added to fix repeated
"Failed to write audit log: [Errno 22] Invalid argument" failures. Root
cause (see data/audit.py::_rotate_if_oversized docstring): a 131 MB
audit.log that had never been rotated, inside a cloud-synced project
directory, reliably failed open+close — not an invalid path/filename, and
not fixed by switching text/binary mode; only rotating it off first
resolved the failure. These tests prove rotation triggers before that
failure mode can recur, and that audit writes remain non-fatal.
"""
from __future__ import annotations

import json
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-audit-secret-long-enough-value-1234567890")
os.environ.setdefault("USER_ID_SALT", "test-audit-salt-long-enough-value-1234567890")

import data.audit as audit_module
import data.models as models


def _init_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "audit.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()


def _use_tmp_log(tmp_path, monkeypatch):
    log_path = tmp_path / "audit.log"
    monkeypatch.setattr(audit_module, "AUDIT_LOG_PATH", log_path)
    return log_path


def test_log_audit_event_succeeds_under_normal_conditions(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    log_path = _use_tmp_log(tmp_path, monkeypatch)

    audit_module.log_audit_event(
        {"task_type": "query_execution", "status": "success"}, user_id="u1"
    )

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["task_type"] == "query_execution"
    assert record["status"] == "success"
    assert record["user_id"] == "u1"


def test_log_audit_event_never_logs_raw_original_input(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    log_path = _use_tmp_log(tmp_path, monkeypatch)

    audit_module.log_audit_event(
        {"task_type": "query_execution", "original_input": "secret question text", "status": "success"},
        user_id="u1",
    )

    raw = log_path.read_text(encoding="utf-8")
    assert "secret question text" not in raw  # encrypted, not stored in plaintext


def test_rotation_triggers_before_size_threshold_is_exceeded(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    log_path = _use_tmp_log(tmp_path, monkeypatch)
    monkeypatch.setattr(audit_module, "AUDIT_LOG_MAX_BYTES", 100)  # tiny threshold, deterministic

    log_path.write_text("x" * 200, encoding="utf-8")  # pre-existing oversized file

    audit_module.log_audit_event({"task_type": "t", "status": "ok"}, user_id="u1")

    # Old content archived, not deleted.
    archives = list(tmp_path.glob("audit.log.*.bak"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "x" * 200

    # A fresh, small file now holds only the new write.
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_rotation_does_not_trigger_below_threshold(tmp_path, monkeypatch):
    _init_db(tmp_path, monkeypatch)
    log_path = _use_tmp_log(tmp_path, monkeypatch)
    monkeypatch.setattr(audit_module, "AUDIT_LOG_MAX_BYTES", 100_000)

    log_path.write_text("existing line\n", encoding="utf-8")

    audit_module.log_audit_event({"task_type": "t", "status": "ok"}, user_id="u1")

    assert not list(tmp_path.glob("audit.log.*.bak"))
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == "existing line"


def test_audit_failure_is_non_fatal(tmp_path, monkeypatch):
    """A write failure at either layer must never raise out of
    log_audit_event — it is a fire-and-forget side effect of execution,
    never allowed to break the caller's real work."""
    _init_db(tmp_path, monkeypatch)
    _use_tmp_log(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(audit_module, "_rotate_if_oversized", lambda: None)
    monkeypatch.setattr("builtins.open", _boom)

    audit_module.log_audit_event({"task_type": "t", "status": "ok"}, user_id="u1")  # must not raise
