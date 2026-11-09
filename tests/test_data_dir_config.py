"""
Tests for TOOLSMITH_DATA_DIR path resolution — Phase 3.2 / Task 1.

Covers data.db._resolve_db_path and data.audit._resolve_audit_log_path in
isolation (pure functions of their arguments, not os.getenv() reads), so
resolution logic is verified without needing to reload either module or
mutate process environment variables.
"""
from __future__ import annotations

import pathlib

import data.audit as audit_module
import data.db as db_module


def test_db_path_defaults_next_to_module_when_data_dir_unset():
    resolved = db_module._resolve_db_path("")
    assert resolved == pathlib.Path(db_module.__file__).parent / "toolsmith.db"


def test_db_path_uses_data_dir_when_set(tmp_path):
    resolved = db_module._resolve_db_path(str(tmp_path))
    assert resolved == tmp_path / "toolsmith.db"


def test_audit_log_path_defaults_next_to_module_when_both_unset():
    resolved = audit_module._resolve_audit_log_path("", "")
    assert resolved == pathlib.Path(audit_module.__file__).parent / "audit.log"


def test_audit_log_path_uses_data_dir_when_audit_log_dir_unset(tmp_path):
    resolved = audit_module._resolve_audit_log_path("", str(tmp_path))
    assert resolved == tmp_path / "audit.log"


def test_audit_log_dir_takes_precedence_over_data_dir(tmp_path):
    audit_dir = tmp_path / "audit_only"
    data_dir = tmp_path / "data_dir"
    resolved = audit_module._resolve_audit_log_path(str(audit_dir), str(data_dir))
    assert resolved == audit_dir / "audit.log"
    assert resolved != data_dir / "audit.log"


def test_module_level_constants_use_the_same_resolution_functions():
    """The bound constants every caller actually imports must be produced by
    the same resolver the tests above exercise directly — not a second,
    divergent inline expression."""
    assert db_module.DB_PATH == db_module._resolve_db_path(
        __import__("core.config", fromlist=["TOOLSMITH_DATA_DIR"]).TOOLSMITH_DATA_DIR
    )
    from core.config import AUDIT_LOG_DIR, TOOLSMITH_DATA_DIR
    assert audit_module.AUDIT_LOG_PATH == audit_module._resolve_audit_log_path(
        AUDIT_LOG_DIR, TOOLSMITH_DATA_DIR
    )
