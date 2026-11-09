"""
Tests for data/migrate_data_dir.py — Phase 3.2 / Task 1.

Exercises migrate() directly against small fake files under tmp_path (never
the real toolsmith.db) — verifies the copy-not-move contract, size
verification, and force/no-force overwrite behavior.
"""
from __future__ import annotations

import sqlite3

import data.migrate_data_dir as migrate_module


def _make_sqlite_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


def test_migrate_copies_db_and_leaves_original_in_place(tmp_path, monkeypatch):
    src_db = tmp_path / "src" / "toolsmith.db"
    src_db.parent.mkdir()
    _make_sqlite_db(src_db)
    audit_log = tmp_path / "src" / "audit.log"
    audit_log.write_text("line1\n", encoding="utf-8")

    monkeypatch.setattr(migrate_module, "DB_PATH", src_db)
    monkeypatch.setattr(migrate_module, "AUDIT_LOG_PATH", audit_log)

    dest_dir = tmp_path / "dest"
    results = migrate_module.migrate(dest_dir)

    assert (dest_dir / "toolsmith.db").exists()
    assert (dest_dir / "audit.log").read_text(encoding="utf-8") == "line1\n"
    # Original untouched — migrate() copies, never moves or deletes.
    assert src_db.exists()
    assert audit_log.exists()
    assert any("copied" in r for r in results)


def test_migrate_skips_existing_destination_without_force(tmp_path, monkeypatch):
    src_db = tmp_path / "src" / "toolsmith.db"
    src_db.parent.mkdir()
    _make_sqlite_db(src_db)
    monkeypatch.setattr(migrate_module, "DB_PATH", src_db)
    monkeypatch.setattr(migrate_module, "AUDIT_LOG_PATH", tmp_path / "src" / "audit.log")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "toolsmith.db").write_text("pre-existing", encoding="utf-8")

    results = migrate_module.migrate(dest_dir)

    assert (dest_dir / "toolsmith.db").read_text(encoding="utf-8") == "pre-existing"
    assert any("skip" in r and "already exists" in r for r in results)


def test_migrate_force_overwrites_existing_destination(tmp_path, monkeypatch):
    src_db = tmp_path / "src" / "toolsmith.db"
    src_db.parent.mkdir()
    _make_sqlite_db(src_db)
    monkeypatch.setattr(migrate_module, "DB_PATH", src_db)
    monkeypatch.setattr(migrate_module, "AUDIT_LOG_PATH", tmp_path / "src" / "audit.log")

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "toolsmith.db").write_text("stale", encoding="utf-8")

    migrate_module.migrate(dest_dir, force=True)

    # Overwritten with the real sqlite file (larger than the 5-byte stub).
    assert (dest_dir / "toolsmith.db").stat().st_size > 5


def test_migrate_skips_missing_source_files_gracefully(tmp_path, monkeypatch):
    missing_db = tmp_path / "does_not_exist" / "toolsmith.db"
    monkeypatch.setattr(migrate_module, "DB_PATH", missing_db)
    monkeypatch.setattr(migrate_module, "AUDIT_LOG_PATH", tmp_path / "does_not_exist" / "audit.log")

    results = migrate_module.migrate(tmp_path / "dest")

    assert all("skip" in r and "does not exist" in r for r in results)
