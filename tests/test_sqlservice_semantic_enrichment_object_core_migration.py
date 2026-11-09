"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4C2, Part 5:
migration 016 (add `phase` to sql_semantic_enrichments) and its
data/models.py fresh-database equivalence.

NEVER applies anything against data/toolsmith.db or any live database --
every connection here is an in-memory or temporary on-disk SQLite database
created fresh by this test file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_migration.py -v
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4c2-migration-secret-long-enough-1234")
os.environ.setdefault("USER_ID_SALT", "test-step4c2-migration-salt-long-enough-12345")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATION_015 = (_PROJECT_ROOT / "migrations" / "015_create_semantic_enrichment.sql").read_text(encoding="utf-8")
_MIGRATION_016 = (_PROJECT_ROOT / "migrations" / "016_add_semantic_enrichment_phase.sql").read_text(encoding="utf-8")


def _minimal_source_table(conn: sqlite3.Connection) -> None:
    # sql_semantic_enrichments has an FK to data_source_connections -- a
    # minimal stand-in is enough for these schema-shape tests (full
    # ownership behavior is already covered by the store's own test suite).
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    return {
        row["name"]: {"type": row["type"], "notnull": row["notnull"], "dflt_value": row["dflt_value"]}
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA index_list({table})").fetchall()}


def _unique_index_columns(conn: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall())


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


# ── Migration 016 applies cleanly on top of 015 ─────────────────────────


def test_016_applies_cleanly_on_top_of_015_and_adds_phase_column(conn):
    _minimal_source_table(conn)
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)

    columns = _table_columns(conn, "sql_semantic_enrichments")
    assert "phase" in columns
    assert columns["phase"]["notnull"] == 1
    assert columns["phase"]["dflt_value"] == "'COLUMN_DEFERRED'"


def test_016_backfills_pre_existing_rows_with_column_deferred(conn):
    _minimal_source_table(conn)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u')")
    conn.executescript(_MIGRATION_015)

    h = "a" * 64
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, "
        " payload_json, status, created_at, updated_at) VALUES (1, 'dbo.T', ?, 'p', 'm', 1, '{}', 'ACTIVE', 't', 't')",
        (h,),
    )
    conn.commit()

    conn.executescript(_MIGRATION_016)

    row = conn.execute("SELECT phase FROM sql_semantic_enrichments WHERE object_fqn = 'dbo.T'").fetchone()
    assert row["phase"] == "COLUMN_DEFERRED"


def test_016_check_constraint_rejects_invalid_phase(conn):
    _minimal_source_table(conn)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u')")
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)

    h = "b" * 64
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO sql_semantic_enrichments "
            "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
            " payload_json, status, created_at, updated_at) "
            "VALUES (1, 'dbo.T', ?, 'p', 'm', 1, 'NOT_A_PHASE', '{}', 'ACTIVE', 't', 't')",
            (h,),
        )


def test_016_replaces_unique_index_to_include_phase(conn):
    _minimal_source_table(conn)
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)

    assert "idx_sse_cache_key" in _index_names(conn, "sql_semantic_enrichments")
    columns = _unique_index_columns(conn, "idx_sse_cache_key")
    assert columns == (
        "source_id", "object_fqn", "semantic_input_hash", "provider_id", "model", "contract_version", "phase",
    )


def test_016_lets_object_core_and_column_deferred_rows_coexist_at_same_hash(conn):
    """The whole POINT of the phase-inclusive unique index: two rows that
    would have collided under migration 015's OLD index (same source/
    object/hash/provider/model/contract_version) can coexist once `phase`
    differs."""
    _minimal_source_table(conn)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u')")
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)

    h = "c" * 64
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) "
        "VALUES (1, 'dbo.T', ?, 'p', 'm', 1, 'OBJECT_CORE', '{}', 'ACTIVE', 't', 't')",
        (h,),
    )
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) "
        "VALUES (1, 'dbo.T', ?, 'p', 'm', 1, 'COLUMN_DEFERRED', '{}', 'ACTIVE', 't', 't')",
        (h,),
    )
    conn.commit()
    rows = conn.execute("SELECT phase FROM sql_semantic_enrichments WHERE object_fqn = 'dbo.T'").fetchall()
    assert {r["phase"] for r in rows} == {"OBJECT_CORE", "COLUMN_DEFERRED"}


# ── Idempotency: SQLite ALTER TABLE ADD COLUMN has no "IF NOT EXISTS" ────


def test_016_reapplication_fails_loudly_without_corrupting_existing_data(conn):
    _minimal_source_table(conn)
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u')")
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)

    h = "d" * 64
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) "
        "VALUES (1, 'dbo.T', ?, 'p', 'm', 1, 'OBJECT_CORE', '{}', 'ACTIVE', 't', 't')",
        (h,),
    )
    conn.commit()

    with pytest.raises(sqlite3.OperationalError, match="duplicate column"):
        conn.executescript(_MIGRATION_016)

    # The pre-existing row and schema must be entirely intact after the
    # failed re-application -- SQLite DDL failures don't corrupt a table.
    row = conn.execute("SELECT phase FROM sql_semantic_enrichments WHERE object_fqn = 'dbo.T'").fetchone()
    assert row["phase"] == "OBJECT_CORE"
    assert "phase" in _table_columns(conn, "sql_semantic_enrichments")


# ── data/models.py fresh-database equivalence ────────────────────────────


def test_fresh_init_db_schema_matches_015_plus_016_end_state(conn, tmp_path, monkeypatch):
    """A brand-new database created via data.models.init_db() must end up
    schema-identical to an existing database that had 015 THEN 016 applied
    -- same column set/defaults, same phase-inclusive unique index."""
    _minimal_source_table(conn)
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)
    migrated_columns = _table_columns(conn, "sql_semantic_enrichments")
    migrated_unique_index_columns = _unique_index_columns(conn, "idx_sse_cache_key")

    import data.models as models_module

    db_path = str(tmp_path / "fresh.db")
    original_get_connection = models_module.get_connection
    fresh_conn_holder = {}

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        fresh_conn_holder["conn"] = c
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    try:
        models_module.init_db()
    finally:
        monkeypatch.setattr(models_module, "get_connection", original_get_connection)

    fresh_conn = sqlite3.connect(db_path)
    fresh_conn.row_factory = sqlite3.Row
    try:
        fresh_columns = _table_columns(fresh_conn, "sql_semantic_enrichments")
        fresh_unique_index_columns = _unique_index_columns(fresh_conn, "idx_sse_cache_key")
        assert set(fresh_columns.keys()) == set(migrated_columns.keys())
        assert fresh_columns["phase"]["dflt_value"] == migrated_columns["phase"]["dflt_value"]
        assert fresh_unique_index_columns == migrated_unique_index_columns
    finally:
        fresh_conn.close()
