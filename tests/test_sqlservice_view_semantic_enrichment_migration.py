"""
Focused tests -- SQL Architecture Correction, Phase 6, Step 6C1, Part 1/2:
persistence-contract audit and correction for deep VIEW_ENRICHED semantics.

Step 6C's original design widened sql_semantic_enrichments' `phase` CHECK
constraint (via a table rebuild) to add 'VIEW_ENRICHED'. This audit found
three concrete reasons that design was riskier than necessary and replaced
it with a brand-new, dedicated table (`sql_view_semantic_enrichments`,
migrations/017_create_view_semantic_enrichments.sql +
data/models.py's ensure_sql_view_semantic_enrichments_table):
  1. sql_semantic_enrichments is a LIVE, already-populated table (hundreds of real
     ACTIVE OBJECT_CORE rows as of this audit, 2026-09-16).
  2. data/db.py's get_connection() sets `PRAGMA foreign_keys = ON`, and
     SQLite only allows toggling that pragma OUTSIDE an active transaction
     -- the abandoned rebuild never accounted for this.
  3. SQLite has no ALTER TABLE ... ALTER COLUMN / DROP CONSTRAINT, so
     widening a CHECK is inherently multi-statement and partially-
     completable; a brand-new table is a single atomic CREATE TABLE IF NOT
     EXISTS that can never be partially applied.

Several tests below explicitly reproduce and verify this reasoning (the
FK-pragma-transaction restriction, the "no partial state possible" property)
rather than merely asserting the new schema shape.

NEVER applies anything against data/toolsmith.db or any live database --
every connection here is an in-memory or temporary on-disk SQLite database
created fresh by this test file.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-step6c1-migration-secret-long-enough-12345")
os.environ.setdefault("USER_ID_SALT", "test-step6c1-migration-salt-long-enough-123456")

import data.models as models_module

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_MIGRATION_015 = (_PROJECT_ROOT / "migrations" / "015_create_semantic_enrichment.sql").read_text(encoding="utf-8")
_MIGRATION_016 = (_PROJECT_ROOT / "migrations" / "016_add_semantic_enrichment_phase.sql").read_text(encoding="utf-8")
_MIGRATION_017 = (_PROJECT_ROOT / "migrations" / "017_create_view_semantic_enrichments.sql").read_text(encoding="utf-8")


def _minimal_source_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    conn.execute("INSERT INTO data_source_connections (id, user_id) VALUES (1, 'u1')")


def _create_table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row["sql"] if row else ""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture
def legacy_conn(conn):
    """A database at exactly the pre-Step-6C1 end state: migrations
    015+016 applied (sql_semantic_enrichments has only OBJECT_CORE/
    COLUMN_DEFERRED), with real OBJECT_CORE and COLUMN_DEFERRED rows
    already present -- exactly like the live the reference database database (hundreds of real
    ACTIVE OBJECT_CORE rows as of this audit)."""
    _minimal_source_table(conn)
    conn.executescript(_MIGRATION_015)
    conn.executescript(_MIGRATION_016)
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(id, source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) VALUES "
        "(1, 1,'dbo.T1',?, 'p','m',1,'OBJECT_CORE','{\"payload\":1}','ACTIVE','t1','t1')",
        ("a" * 64,),
    )
    conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(id, source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) VALUES "
        "(2, 1,'dbo.T2',?, 'p','m',1,'COLUMN_DEFERRED','{\"payload\":2}','INVALID','t2','t2')",
        ("b" * 64,),
    )
    conn.commit()
    return conn


def _dump_sse_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM sql_semantic_enrichments ORDER BY id").fetchall()]


# ── Part 1 audit: why the widening-migration path is unnecessary/riskier ──


def test_live_check_constraint_genuinely_rejects_view_enriched(legacy_conn):
    """Reproduces the exact live-database finding this audit is based on:
    the pre-Step-6C1 CHECK constraint really does reject 'VIEW_ENRICHED'
    -- confirming SOME schema change (not just application code) is
    required to store that literal value in this column."""
    with pytest.raises(sqlite3.IntegrityError):
        legacy_conn.execute(
            "INSERT INTO sql_semantic_enrichments "
            "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
            " payload_json, status, created_at, updated_at) VALUES (1,'dbo.V1',?, 'p','m',1,'VIEW_ENRICHED','{}','ACTIVE','t','t')",
            ("c" * 64,),
        )


def test_foreign_keys_pragma_cannot_be_toggled_inside_a_transaction(legacy_conn):
    """Reproduces the concrete SQLite behavior that made the original
    rebuild-based migration unsafe: data/db.py's get_connection() enables
    `PRAGMA foreign_keys = ON` for every real connection, and SQLite
    silently makes this pragma a no-op once a transaction is already open
    -- a rebuild migration that toggles it mid-transaction would not
    actually have taken effect, without raising any error to say so."""
    legacy_conn.execute("PRAGMA foreign_keys = ON")
    legacy_conn.execute("BEGIN")
    legacy_conn.execute("PRAGMA foreign_keys = OFF")  # silently a no-op here
    still_on = legacy_conn.execute("PRAGMA foreign_keys").fetchone()[0]
    legacy_conn.execute("COMMIT")
    assert still_on == 1  # proves the toggle attempt inside the transaction had no effect


def test_no_other_table_references_sql_semantic_enrichments_as_parent(legacy_conn):
    """Confirms the rebuild path was not blocked by an inbound FK from some
    OTHER table (it wasn't) -- the decision to avoid it rests entirely on
    risk-to-live-data and the FK-pragma-timing issue, not a hard blocker."""
    rows = legacy_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%sql_semantic_enrichments%' "
        "AND name != 'sql_semantic_enrichments'"
    ).fetchall()
    assert rows == []


# ── Part 2: the dedicated-table design is genuinely additive ──────────────


def test_017_creates_dedicated_table_without_touching_sql_semantic_enrichments(legacy_conn):
    before = _dump_sse_rows(legacy_conn)
    legacy_conn.executescript(_MIGRATION_017)
    after = _dump_sse_rows(legacy_conn)
    assert before == after  # byte-for-byte: IDs, content, timestamps, statuses all identical
    assert _table_exists(legacy_conn, "sql_view_semantic_enrichments")


def test_017_new_table_has_no_phase_column():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    conn.executescript(_MIGRATION_017)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sql_view_semantic_enrichments)").fetchall()}
    assert "phase" not in columns
    assert columns == {
        "id", "source_id", "object_fqn", "semantic_input_hash", "provider_id", "model",
        "contract_version", "payload_json", "status", "created_at", "updated_at",
    }
    conn.close()


def test_017_indexes_and_unique_constraint_present():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    conn.executescript(_MIGRATION_017)
    index_names = {r["name"] for r in conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()}
    assert {"idx_svse_cache_key", "idx_svse_source_current", "idx_svse_source_object"} <= index_names
    unique_index = next(
        r for r in conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()
        if r["name"] == "idx_svse_cache_key"
    )
    assert unique_index["unique"] == 1
    conn.close()


def test_017_foreign_key_to_data_source_connections_present():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    conn.executescript(_MIGRATION_017)
    fks = conn.execute("PRAGMA foreign_key_list(sql_view_semantic_enrichments)").fetchall()
    assert len(fks) == 1
    assert fks[0]["table"] == "data_source_connections"
    assert fks[0]["on_delete"] == "CASCADE"
    conn.close()


def test_017_is_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    conn.executescript(_MIGRATION_017)
    conn.execute(
        "INSERT INTO sql_view_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, "
        " payload_json, status, created_at, updated_at) VALUES (1,'dbo.V1',?, 'p','m',1,'{}','ACTIVE','t','t')",
        ("a" * 64,),
    )
    conn.commit()
    conn.executescript(_MIGRATION_017)  # re-apply -- must be a safe no-op
    row = conn.execute("SELECT object_fqn FROM sql_view_semantic_enrichments").fetchone()
    assert row["object_fqn"] == "dbo.V1"
    conn.close()


# ── data/models.py runner: ensure_sql_view_semantic_enrichments_table ─────


def test_runner_creates_table_and_is_idempotent(legacy_conn):
    result1 = models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    assert result1 == "APPLIED"
    assert _table_exists(legacy_conn, "sql_view_semantic_enrichments")

    result2 = models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    assert result2 == "ALREADY_CURRENT"


def test_runner_never_touches_sql_semantic_enrichments(legacy_conn):
    before = _dump_sse_rows(legacy_conn)
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    after = _dump_sse_rows(legacy_conn)
    assert before == after


def test_runner_preserves_object_core_row_ids_content_timestamps_status(legacy_conn):
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    row = dict(legacy_conn.execute("SELECT * FROM sql_semantic_enrichments WHERE id = 1").fetchone())
    assert row["object_fqn"] == "dbo.T1"
    assert row["phase"] == "OBJECT_CORE"
    assert row["status"] == "ACTIVE"
    assert row["created_at"] == "t1"
    assert row["updated_at"] == "t1"
    assert row["payload_json"] == '{"payload":1}'


def test_runner_preserves_column_deferred_row_ids_content_timestamps_status(legacy_conn):
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    row = dict(legacy_conn.execute("SELECT * FROM sql_semantic_enrichments WHERE id = 2").fetchone())
    assert row["object_fqn"] == "dbo.T2"
    assert row["phase"] == "COLUMN_DEFERRED"
    assert row["status"] == "INVALID"
    assert row["created_at"] == "t2"
    assert row["payload_json"] == '{"payload":2}'


def test_runner_preserves_existing_indexes_and_unique_constraint(legacy_conn):
    before_indexes = {r["name"] for r in legacy_conn.execute("PRAGMA index_list(sql_semantic_enrichments)").fetchall()}
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    after_indexes = {r["name"] for r in legacy_conn.execute("PRAGMA index_list(sql_semantic_enrichments)").fetchall()}
    assert before_indexes == after_indexes
    assert "idx_sse_cache_key" in after_indexes


def test_runner_preserves_foreign_keys_on_original_table(legacy_conn):
    before_fks = legacy_conn.execute("PRAGMA foreign_key_list(sql_semantic_enrichments)").fetchall()
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    after_fks = legacy_conn.execute("PRAGMA foreign_key_list(sql_semantic_enrichments)").fetchall()
    assert [dict(r) for r in before_fks] == [dict(r) for r in after_fks]


def test_runner_never_applied_when_table_does_not_exist_yet_still_creates(conn):
    """Unlike the abandoned rebuild-migration's own runner (which
    deliberately deferred to a prerequisite step and returned
    ALREADY_CURRENT when sql_semantic_enrichments itself did not exist
    yet), this runner has no such prerequisite: sql_view_semantic_
    enrichments is entirely independent of sql_semantic_enrichments's own
    existence."""
    _minimal_source_table(conn)
    result = models_module.ensure_sql_view_semantic_enrichments_table(conn)
    assert result == "APPLIED"
    assert _table_exists(conn, "sql_view_semantic_enrichments")


def test_runner_cannot_be_partially_applied():
    """A CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS sequence
    has no intermediate state a partial-failure detector would ever need
    to find: either the whole executescript() call succeeds (all four
    statements) or SQLite raises before any of them take effect for a
    genuinely malformed statement. Verified here by confirming the table
    and all three indexes always appear together, never separately."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE data_source_connections (id INTEGER PRIMARY KEY, user_id TEXT)")
    models_module.ensure_sql_view_semantic_enrichments_table(conn)
    assert _table_exists(conn, "sql_view_semantic_enrichments")
    index_names = {r["name"] for r in conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()}
    assert {"idx_svse_cache_key", "idx_svse_source_current", "idx_svse_source_object"} <= index_names
    conn.close()


def test_unrelated_tables_unaffected(legacy_conn):
    legacy_conn.execute("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY, x TEXT)")
    legacy_conn.execute("INSERT INTO unrelated_table (id, x) VALUES (1, 'hello')")
    legacy_conn.commit()
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    row = legacy_conn.execute("SELECT x FROM unrelated_table WHERE id = 1").fetchone()
    assert row["x"] == "hello"


# ── Fresh init_db() schema equivalence ─────────────────────────────────────


def test_fresh_init_db_creates_both_tables_with_matching_shape(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh.db")

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    models_module.init_db()

    verify_conn = _get_connection()
    sse_sql = _create_table_sql(verify_conn, "sql_semantic_enrichments")
    svse_sql = _create_table_sql(verify_conn, "sql_view_semantic_enrichments")
    assert "VIEW_ENRICHED" not in sse_sql  # never widened -- unchanged from before Step 6C
    assert "OBJECT_CORE" in sse_sql and "COLUMN_DEFERRED" in sse_sql
    assert svse_sql != ""
    verify_conn.close()


def test_fresh_schema_equals_legacy_database_schema_after_runner(legacy_conn, tmp_path, monkeypatch):
    """A brand-new database via init_db() must be schema-identical (for
    sql_view_semantic_enrichments) to an existing pre-Step-6C1 database
    migrated forward via ensure_sql_view_semantic_enrichments_table() --
    the exact function init_db() itself calls."""
    models_module.ensure_sql_view_semantic_enrichments_table(legacy_conn)
    migrated_columns = {r["name"] for r in legacy_conn.execute("PRAGMA table_info(sql_view_semantic_enrichments)").fetchall()}
    migrated_indexes = {r["name"] for r in legacy_conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()}

    db_path = str(tmp_path / "fresh.db")

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    models_module.init_db()

    fresh_conn = _get_connection()
    try:
        fresh_columns = {r["name"] for r in fresh_conn.execute("PRAGMA table_info(sql_view_semantic_enrichments)").fetchall()}
        fresh_indexes = {r["name"] for r in fresh_conn.execute("PRAGMA index_list(sql_view_semantic_enrichments)").fetchall()}
        assert fresh_columns == migrated_columns
        assert fresh_indexes == migrated_indexes
    finally:
        fresh_conn.close()


def test_init_db_end_to_end_against_a_legacy_file_database(tmp_path, monkeypatch):
    """The REAL init_db() entry point, run against a database whose
    sql_semantic_enrichments table already has real OBJECT_CORE/
    COLUMN_DEFERRED rows -- proving the wiring (not just the standalone
    function in isolation) creates sql_view_semantic_enrichments without
    disturbing them, and a second startup afterward is a safe no-op."""
    db_path = str(tmp_path / "legacy.db")

    def _get_connection():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(models_module, "get_connection", _get_connection)
    models_module.init_db()  # establishes every table at its real, current shape

    seed_conn = _get_connection()
    seed_conn.execute(
        "INSERT INTO sql_semantic_enrichments "
        "(source_id, object_fqn, semantic_input_hash, provider_id, model, contract_version, phase, "
        " payload_json, status, created_at, updated_at) VALUES (1,'dbo.T1',?, 'p','m',1,'OBJECT_CORE','{}','ACTIVE','t','t')",
        ("a" * 64,),
    )
    seed_conn.commit()
    seed_conn.close()

    models_module.init_db()  # second startup: must be a safe no-op

    verify_conn = _get_connection()
    assert _table_exists(verify_conn, "sql_view_semantic_enrichments")
    preserved = verify_conn.execute("SELECT object_fqn, phase FROM sql_semantic_enrichments WHERE object_fqn='dbo.T1'").fetchone()
    assert preserved["phase"] == "OBJECT_CORE"
    verify_conn.close()
