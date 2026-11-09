"""
Tests for core.profile_view.sql_adapter.build_profile_view_from_source().

Phase 1F.2 — migrates the inline `SELECT * FROM profiling_snapshots` lookup
to data.profiling_snapshot_resolver.get_latest_profiling_snapshot_detail().
These tests are regression guards proving that migration reproduced the
prior selection policy and field mapping exactly.

Every test uses an in-memory SQLite DB patched over both
core.profile_view.sql_adapter.get_connection and
data.profiling_snapshot_resolver.get_connection — the resolver holds its own
get_connection reference in its own module namespace, so patching the
adapter alone would leave it hitting the real on-disk DB.

Run from the project root:
    venv/Scripts/pytest tests/test_sql_adapter.py -v
"""
from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from core.profile_view.sql_adapter import build_profile_view_from_source

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT    NOT NULL,
        display_name TEXT    NOT NULL DEFAULT 'Test Source',
        source_type  TEXT    NOT NULL DEFAULT 'mssql'
    );

    CREATE TABLE profiling_snapshots (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id            INTEGER NOT NULL,
        schema_snapshot_id   INTEGER NOT NULL DEFAULT 1,
        snapshot_version     INTEGER NOT NULL DEFAULT 1,
        status               TEXT    NOT NULL DEFAULT 'COMPLETE',
        mode                 TEXT    NOT NULL DEFAULT 'full',
        created_at           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at         TEXT,
        tables_total         INTEGER NOT NULL DEFAULT 0,
        tables_profiled      INTEGER NOT NULL DEFAULT 0,
        columns_profiled     INTEGER NOT NULL DEFAULT 0,
        pii_columns_found    INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled  INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE profiling_table_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        table_name            TEXT    NOT NULL,
        schema_name           TEXT    NOT NULL,
        exact_row_count       INTEGER,
        estimated_row_count   INTEGER,
        column_count          INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE profiling_column_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        column_name           TEXT    NOT NULL,
        ordinal_position      INTEGER NOT NULL DEFAULT 0,
        data_type             TEXT,
        semantic_type         TEXT,
        null_count            INTEGER,
        distinct_count        INTEGER,
        min_value             TEXT,
        max_value             TEXT
    );

    CREATE TABLE profiling_value_samples (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_column_profile_id INTEGER NOT NULL,
        sample_type                 TEXT    NOT NULL,
        value                       TEXT,
        rank                        INTEGER
    );
"""


class _NoClose:
    """Wraps sqlite3.Connection and suppresses close() so the in-memory DB
    survives across multiple get_connection() calls within a single test."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def db():
    """Fresh in-memory DB. Also patches data.profiling_snapshot_resolver.get_connection:
    build_profile_view_from_source() delegates snapshot selection to
    get_latest_profiling_snapshot_detail(), which holds its own get_connection
    reference in that module's namespace."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    wrapper = _NoClose(conn)
    with patch("core.profile_view.sql_adapter.get_connection", return_value=wrapper), \
         patch("data.profiling_snapshot_resolver.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _src(db, user: str = "u1") -> int:
    cur = db.execute(
        "INSERT INTO data_source_connections (user_id, display_name, source_type) "
        "VALUES (?, 'My Source', 'mssql')",
        (user,),
    )
    db.commit()
    return cur.lastrowid


def _snap(
    db,
    source_id: int,
    *,
    snapshot_version: int = 1,
    status: str = "COMPLETE",
    mode: str = "full",
    created_at: str = "2026-01-01T00:00:00",
    completed_at: str | None = "2026-01-01T01:00:00",
    tables_total: int = 0,
    tables_profiled: int = 0,
    columns_profiled: int = 0,
    pii_columns_found: int = 0,
    total_rows_profiled: int = 0,
) -> int:
    cur = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, snapshot_version, status, mode, created_at, completed_at, "
        "tables_total, tables_profiled, columns_profiled, pii_columns_found, total_rows_profiled) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id, snapshot_version, status, mode, created_at, completed_at,
            tables_total, tables_profiled, columns_profiled, pii_columns_found, total_rows_profiled,
        ),
    )
    db.commit()
    return cur.lastrowid


def _table(db, snap_id: int, fqn: str = "dbo.Orders", *,
           exact_row_count: int | None = 100, estimated_row_count: int | None = None,
           column_count: int = 2) -> None:
    schema, name = fqn.split(".", 1)
    db.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, table_fqn, table_name, schema_name, "
        "exact_row_count, estimated_row_count, column_count) "
        "VALUES (?,?,?,?,?,?,?)",
        (snap_id, fqn, name, schema, exact_row_count, estimated_row_count, column_count),
    )
    db.commit()


def _col(db, snap_id: int, fqn: str, col: str, *, ordinal_position: int = 1) -> int:
    cur = db.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, table_fqn, column_name, ordinal_position, data_type) "
        "VALUES (?,?,?,?,'TEXT')",
        (snap_id, fqn, col, ordinal_position),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# 1. Resolver delegation
# ---------------------------------------------------------------------------

def test_resolver_delegation_builds_profile_from_resolved_snapshot(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")

    assert profile is not None
    assert profile.summary_metrics["snapshot_id"] == snap
    assert len(profile.tables) == 1
    assert profile.tables[0].table_fqn == "dbo.Orders"


# ---------------------------------------------------------------------------
# 2. ValueError preserved when no snapshot exists
# ---------------------------------------------------------------------------

def test_no_snapshot_raises_value_error(db):
    src = _src(db)
    with pytest.raises(ValueError, match=f"source id={src}"):
        build_profile_view_from_source(src, "u1")


def test_wrong_user_returns_none_not_value_error(db):
    src = _src(db, "alice")
    assert build_profile_view_from_source(src, "bob") is None


# ---------------------------------------------------------------------------
# 3. generated_at behavior preserved
# ---------------------------------------------------------------------------

def test_generated_at_falls_back_to_created_at_when_completed_at_null(db):
    src = _src(db)
    snap = _snap(db, src, created_at="2026-01-01T00:00:00", completed_at=None)
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")
    assert profile.generated_at == "2026-01-01T00:00:00"


def test_generated_at_prefers_completed_at_over_created_at(db):
    src = _src(db)
    snap = _snap(db, src, created_at="2026-01-01T00:00:00", completed_at="2026-01-02T12:00:00")
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")
    assert profile.generated_at == "2026-01-02T12:00:00"


# ---------------------------------------------------------------------------
# 4. Snapshot counters preserved
# ---------------------------------------------------------------------------

def test_snapshot_counters_preserved_in_summary(db):
    src = _src(db)
    snap = _snap(
        db, src,
        tables_total=5, tables_profiled=4, columns_profiled=20,
        pii_columns_found=3, total_rows_profiled=98765,
    )
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")
    s = profile.summary_metrics
    assert s["total_tables"] == 5
    assert s["tables_profiled"] == 4
    assert s["columns_profiled"] == 20
    assert s["pii_columns_found"] == 3
    assert s["total_rows_profiled"] == 98765


def test_total_tables_falls_back_to_len_table_rows_when_zero(db):
    src = _src(db)
    snap = _snap(db, src, tables_total=0)
    _table(db, snap, "dbo.Orders")
    _table(db, snap, "dbo.Customers")

    profile = build_profile_view_from_source(src, "u1")
    assert profile.summary_metrics["total_tables"] == 2


# ---------------------------------------------------------------------------
# 5. Status preserved
# ---------------------------------------------------------------------------

def test_profiling_status_preserved(db):
    src = _src(db)
    snap = _snap(db, src, status="RUNNING")
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")
    assert profile.summary_metrics["profiling_status"] == "RUNNING"


# ---------------------------------------------------------------------------
# 6. Mode preserved
# ---------------------------------------------------------------------------

def test_profiling_mode_preserved(db):
    src = _src(db)
    snap = _snap(db, src, mode="STRUCTURAL_ONLY")
    _table(db, snap, "dbo.Orders")

    profile = build_profile_view_from_source(src, "u1")
    assert profile.summary_metrics["profiling_mode"] == "STRUCTURAL_ONLY"


# ---------------------------------------------------------------------------
# 7. Latest snapshot_version still wins
# ---------------------------------------------------------------------------

def test_latest_snapshot_version_still_wins(db):
    src = _src(db)
    older = _snap(db, src, snapshot_version=1)
    _table(db, older, "dbo.Old")

    newer = _snap(db, src, snapshot_version=2)
    _table(db, newer, "dbo.New")

    profile = build_profile_view_from_source(src, "u1")
    fqns = {t.table_fqn for t in profile.tables}
    assert fqns == {"dbo.New"}
    assert profile.summary_metrics["snapshot_version"] == 2


# ---------------------------------------------------------------------------
# 8. Older COMPLETE beats newer CANCELLED
# ---------------------------------------------------------------------------

def test_older_complete_snapshot_beats_newer_cancelled(db):
    src = _src(db)
    older = _snap(db, src, snapshot_version=1, status="COMPLETE")
    _table(db, older, "dbo.Old")

    newer = _snap(db, src, snapshot_version=2, status="CANCELLED")
    _table(db, newer, "dbo.New")

    profile = build_profile_view_from_source(src, "u1")
    # The older COMPLETE snapshot must win selection — a newer CANCELLED
    # snapshot must never shadow older, usable COMPLETE evidence.
    fqns = {t.table_fqn for t in profile.tables}
    assert fqns == {"dbo.Old"}
    assert profile.summary_metrics["profiling_status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 9. Older COMPLETE beats newer RUNNING
# ---------------------------------------------------------------------------

def test_older_complete_snapshot_beats_newer_running(db):
    src = _src(db)
    older = _snap(db, src, snapshot_version=1, status="COMPLETE")
    _table(db, older, "dbo.Old")

    newer = _snap(db, src, snapshot_version=2, status="RUNNING")
    _table(db, newer, "dbo.New")

    profile = build_profile_view_from_source(src, "u1")
    # The older COMPLETE snapshot must win selection — a newer RUNNING
    # snapshot must never shadow older, usable COMPLETE evidence.
    fqns = {t.table_fqn for t in profile.tables}
    assert fqns == {"dbo.Old"}
    assert profile.summary_metrics["profiling_status"] == "COMPLETE"


# ---------------------------------------------------------------------------
# 10. No writes occur
# ---------------------------------------------------------------------------

def test_build_profile_performs_no_writes(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, "dbo.Orders")
    col_id = _col(db, snap, "dbo.Orders", "Email")
    db.execute(
        "INSERT INTO profiling_value_samples (profiling_column_profile_id, sample_type, value, rank) "
        "VALUES (?, 'top', 'a@example.com', 1)",
        (col_id,),
    )
    db.commit()
    # Second source, no snapshot yet, to exercise the ValueError path too.
    src_no_snap = _src(db, "u2")

    tables = (
        "data_source_connections", "profiling_snapshots",
        "profiling_table_profiles", "profiling_column_profiles",
        "profiling_value_samples",
    )
    before = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}

    build_profile_view_from_source(src, "u1")
    with pytest.raises(ValueError):
        build_profile_view_from_source(src_no_snap, "u2")
    build_profile_view_from_source(9999, "nobody")  # nonexistent source -> None path too

    after = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t}").fetchall()] for t in tables}
    assert before == after


# ---------------------------------------------------------------------------
# Extra: field mapping sanity (columns / samples still wired end-to-end)
# ---------------------------------------------------------------------------

def test_column_and_sample_values_still_mapped(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, "dbo.Customers", column_count=1)
    col_id = _col(db, snap, "dbo.Customers", "Email")
    db.execute(
        "INSERT INTO profiling_value_samples (profiling_column_profile_id, sample_type, value, rank) "
        "VALUES (?, 'top', 'a@example.com', 1)",
        (col_id,),
    )
    db.commit()

    profile = build_profile_view_from_source(src, "u1")
    cols = profile.tables[0].columns
    assert len(cols) == 1
    assert cols[0].column_name == "Email"
    assert cols[0].sample_values == ["a@example.com"]
