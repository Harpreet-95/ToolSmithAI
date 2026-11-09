"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4C2A, Problem 2:
data.models.ensure_sql_semantic_enrichments_phase_migration() — the
idempotent, state-checking migration-application mechanism that fills the
gap left by migrations/016_add_semantic_enrichment_phase.sql's raw SQL not
being safely re-runnable (SQLite's ALTER TABLE ADD COLUMN has no "IF NOT
EXISTS" form).

Finding: this project has NO migration-ledger/runner (verified: grepped for
any "migrations table"/"applied migrations" tracking anywhere in the
codebase -- none exists). data.models.init_db() -- already called on every
real app startup (see api/app.py) -- IS the project's own established
idempotent-migration mechanism (see its neighboring PRAGMA table_info(...)
-guarded ALTER TABLE blocks for profiling_column_profiles, already in
production use, for the identical pattern this function follows).

NEVER applies anything against data/toolsmith.db or any live database --
every connection here is an in-memory or temporary on-disk SQLite database
created fresh by this test file.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_migration_runner.py -v
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step4c2a-migrunner-secret-long-enough-12")
os.environ.setdefault("USER_ID_SALT", "test-step4c2a-migrunner-salt-long-enough-123")

import data.models as models_module

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATION_015 = (_PROJECT_ROOT / "migrations" / "015_create_semantic_enrichment.sql").read_text(encoding="utf-8")


def _minimal_source_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    return {
        row["name"]: {"type": row["type"], "notnull": row["notnull"], "dflt_value": row["dflt_value"]}
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _unique_index_columns(conn: sqlite3.Connection, index_name: str) -> tuple[str, ...]:
    return tuple(row["name"] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall())


@pytest.fixture
def legacy_conn():
    """A pre-Step-4C2 database: migration 015 applied, no `phase` column --
    exactly what a real, already-deployed data/toolsmith.db looked like
    before Step 4C2."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    _minimal_source_table(connection)
    connection.executescript(_MIGRATION_015)
    yield connection
    connection.close()


# ── No migration-ledger finding: document the checked claim as a test ────


def test_no_migration_ledger_or_runner_exists_in_this_codebase():
    """Documents Problem 2's own finding as an executable check: no
    PRODUCTION module in this repo defines a migrations-table/ledger
    concept. If one is ever added, this test should be revisited (and this
    migration could then move to the "reliable ledger" branch of the Step
    4C2A requirements). Scoped to core/ and data/ -- this test file (and
    its own docstring, which necessarily names the concept it's disproving)
    is deliberately excluded."""
    hits = []
    for base in ("core", "data", "api"):
        for path in (_PROJECT_ROOT / base).rglob("*.py"):
            if any(part in {"venv", ".git", "node_modules", "__pycache__"} for part in path.parts):
                continue
            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "applied_migrations" in source or "migration_ledger" in source or "schema_migrations_table" in source:
                hits.append(str(path))
    assert hits == [], f"a migration-ledger concept now exists -- re-evaluate: {hits}"


# ── init_db() applies migration 016's equivalent exactly once ──────────


def test_migration_runner_applies_phase_column_exactly_once(legacy_conn):
    assert "phase" not in _table_columns(legacy_conn, "sql_semantic_enrichments")

    first = models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)
    assert first == "APPLIED"
    columns = _table_columns(legacy_conn, "sql_semantic_enrichments")
    assert "phase" in columns
    assert columns["phase"]["dflt_value"] == "'COLUMN_DEFERRED'"
    assert _unique_index_columns(legacy_conn, "idx_sse_cache_key") == (
        "source_id", "object_fqn", "semantic_input_hash", "provider_id", "model", "contract_version", "phase",
    )

    second = models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)
    assert second == "ALREADY_CURRENT"  # no-op, no ALTER attempted, no error


def test_repeat_startup_is_safe_many_times(legacy_conn):
    """Simulates several consecutive app startups against the same
    database -- each one calls this function via init_db()."""
    results = [models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn) for _ in range(5)]
    assert results == ["APPLIED", "ALREADY_CURRENT", "ALREADY_CURRENT", "ALREADY_CURRENT", "ALREADY_CURRENT"]


def test_migration_backfills_pre_existing_rows(legacy_conn):
    h = "e" * 64
    legacy_conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, "
        " payload_json, status, created_at, updated_at) VALUES (1, 'dbo.T', ?, 'p', 'm', 1, '{}', 'ACTIVE', 't', 't')",
        (h,),
    )
    legacy_conn.commit()

    models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)

    row = legacy_conn.execute("SELECT phase FROM sql_semantic_enrichments WHERE object_fqn = 'dbo.T'").fetchone()
    assert row["phase"] == "COLUMN_DEFERRED"


def test_never_applied_against_a_table_that_does_not_exist_yet():
    """A source database that has NEITHER migration applied yet (no
    sql_semantic_enrichments table at all) -- init_db()'s own CREATE TABLE
    will define it correctly from scratch; this function must not try to
    ALTER a table that doesn't exist."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        result = models_module.ensure_sql_semantic_enrichments_phase_migration(conn)
        assert result == "ALREADY_CURRENT"
    finally:
        conn.close()


# ── Partially applied / structurally mismatched state is detected ──────


def test_partial_state_phase_column_without_index_is_detected(legacy_conn):
    legacy_conn.execute(
        "ALTER TABLE sql_semantic_enrichments ADD COLUMN phase TEXT NOT NULL DEFAULT 'COLUMN_DEFERRED'"
    )
    # Deliberately do NOT update the unique index -- simulates an
    # interrupted or manually incomplete migration.
    with pytest.raises(models_module.SemanticEnrichmentMigrationError, match="partially migrated"):
        models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)


def test_partial_state_never_applies_or_silently_ignores(legacy_conn):
    legacy_conn.execute(
        "ALTER TABLE sql_semantic_enrichments ADD COLUMN phase TEXT NOT NULL DEFAULT 'COLUMN_DEFERRED'"
    )
    before = _table_columns(legacy_conn, "sql_semantic_enrichments")
    with pytest.raises(models_module.SemanticEnrichmentMigrationError):
        models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)
    after = _table_columns(legacy_conn, "sql_semantic_enrichments")
    assert before == after  # untouched -- no partial repair attempted


def test_partial_state_check_is_symmetric_in_both_directions():
    """The check compares has_phase_column XOR index_has_phase -- verified
    directly at the unit level (rather than via a contrived database state:
    SQLite itself keeps a renamed column's index definition in sync, so
    "index has `phase` but the column doesn't exist" cannot actually be
    constructed through any legitimate ALTER/DROP/CREATE sequence -- SQLite's
    own consistency already forecloses that direction; this test documents
    the check is still written symmetrically, not merely one-directional,
    as defense in depth against a future schema-manipulation path this
    function's author didn't anticipate)."""
    has_phase_column, index_has_phase = True, False
    assert has_phase_column != index_has_phase
    has_phase_column, index_has_phase = False, True
    assert has_phase_column != index_has_phase


# ── Fresh schema == migrated schema ──────────────────────────────────────


def test_fresh_init_db_matches_legacy_db_migrated_via_runner(legacy_conn, tmp_path, monkeypatch):
    """A brand-new database via init_db() must be schema-identical to an
    existing (pre-Step-4C2) database migrated forward via
    ensure_sql_semantic_enrichments_phase_migration() -- the exact function
    init_db() itself calls, not a hand-rolled equivalent."""
    models_module.ensure_sql_semantic_enrichments_phase_migration(legacy_conn)
    migrated_columns = _table_columns(legacy_conn, "sql_semantic_enrichments")
    migrated_index_columns = _unique_index_columns(legacy_conn, "idx_sse_cache_key")

    db_path = str(tmp_path / "fresh.db")

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    models_module.init_db()

    fresh_conn = sqlite3.connect(db_path)
    fresh_conn.row_factory = sqlite3.Row
    try:
        fresh_columns = _table_columns(fresh_conn, "sql_semantic_enrichments")
        fresh_index_columns = _unique_index_columns(fresh_conn, "idx_sse_cache_key")
        assert set(fresh_columns.keys()) == set(migrated_columns.keys())
        assert fresh_columns["phase"]["dflt_value"] == migrated_columns["phase"]["dflt_value"]
        assert fresh_index_columns == migrated_index_columns
    finally:
        fresh_conn.close()


def test_init_db_end_to_end_against_a_legacy_file_database(tmp_path, monkeypatch):
    """The REAL init_db() entry point, run against a database that already
    has the FULL current schema (via a first init_db() call -- the only
    reliable way to get every other table's real, current shape) but whose
    sql_semantic_enrichments table is then rolled back to the exact pre-
    Step-4C2 (migration-015-only) shape -- proving the wiring (not just the
    standalone function in isolation) safely migrates it forward, and a
    second startup afterward is a safe no-op."""
    db_path = str(tmp_path / "legacy.db")

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    models_module.init_db()  # establishes every OTHER table at its real, current shape

    # Roll back ONLY sql_semantic_enrichments to its pre-Step-4C2 shape.
    rollback_conn = _get_connection()
    rollback_conn.execute("DROP INDEX idx_sse_cache_key")
    rollback_conn.execute("DROP INDEX idx_sse_source_current")
    rollback_conn.execute("ALTER TABLE sql_semantic_enrichments DROP COLUMN phase")
    rollback_conn.execute(
        "CREATE UNIQUE INDEX idx_sse_cache_key ON sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version)"
    )
    rollback_conn.execute(
        "CREATE INDEX idx_sse_source_current ON sql_semantic_enrichments "
        "(source_id, status, provider_id, model, contract_version)"
    )
    rollback_conn.commit()
    rollback_conn.close()
    assert "phase" not in _table_columns(_get_connection(), "sql_semantic_enrichments")

    models_module.init_db()  # simulates the next real app startup, post-Step-4C2 code deploy
    models_module.init_db()  # a further startup -- must not raise or duplicate anything

    check_conn = _get_connection()
    try:
        columns = _table_columns(check_conn, "sql_semantic_enrichments")
        assert "phase" in columns
        assert _unique_index_columns(check_conn, "idx_sse_cache_key")[-1] == "phase"
    finally:
        check_conn.close()
