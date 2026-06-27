"""
Tests for the batch profiling columns_total bug fix.

Bug A: start_batch_profiling did not populate columns_total in the INSERT.
       The DB DEFAULT (0) was stored, leaving every batch snapshot with columns_total=0.

Bug B: continue_batch_profiling never updated columns_total during batch processing,
       so even a completed snapshot kept columns_total=0.

Fix:   columns_total is now seeded with the full expected column count at INSERT time
       inside start_batch_profiling (mirroring how tables_total already works).
       batch_columns_total is computed in continue_batch_profiling for correctness
       verification; columns_total is not accumulated on top of the seeded value
       because that would produce double-counting.

All tests use an in-memory SQLite database patched over data.db.get_connection so
they never touch the real toolsmith.db on disk.

Run from the project root:
    venv/Scripts/pytest tests/test_batch_profiling_columns_total.py -v
"""

import dataclasses
import json
import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-batch-columns-total-long-enough!!")
os.environ.setdefault("USER_ID_SALT",   "test-salt-batch-columns-total")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.profiling.models import ProfilingStatus
from data.profiling_service import (
    get_latest_profile,
    run_structural_profiling,
    start_batch_profiling,
)


# ── Minimal in-memory schema ───────────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test Source',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        source_category       TEXT    NOT NULL DEFAULT 'relational_db',
        encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json     TEXT    NOT NULL DEFAULT '[]',
        metadata_json         TEXT    NOT NULL DEFAULT '{}',
        source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
        is_active             INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE schema_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        source_type      TEXT    NOT NULL DEFAULT 'mssql',
        table_count      INTEGER NOT NULL DEFAULT 0,
        view_count       INTEGER NOT NULL DEFAULT 0,
        column_count     INTEGER NOT NULL DEFAULT 0,
        snapshot_json    TEXT    NOT NULL DEFAULT '{}',
        discovered_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_snapshots (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id                INTEGER NOT NULL,
        schema_snapshot_id       INTEGER NOT NULL DEFAULT 1,
        snapshot_version         INTEGER NOT NULL DEFAULT 1,
        mode                     TEXT    NOT NULL DEFAULT 'structural_only',
        sample_rate              REAL    NOT NULL DEFAULT 1.0,
        profiling_rules_version  TEXT    NOT NULL DEFAULT '4.0.0',
        status                   TEXT    NOT NULL DEFAULT 'PENDING',
        tables_total             INTEGER NOT NULL DEFAULT 0,
        tables_profiled          INTEGER NOT NULL DEFAULT 0,
        tables_skipped           INTEGER NOT NULL DEFAULT 0,
        tables_failed            INTEGER NOT NULL DEFAULT 0,
        tables_timed_out         INTEGER NOT NULL DEFAULT 0,
        columns_total            INTEGER NOT NULL DEFAULT 0,
        columns_profiled         INTEGER NOT NULL DEFAULT 0,
        columns_skipped          INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled      INTEGER NOT NULL DEFAULT 0,
        pii_columns_found        INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at               TEXT,
        completed_at             TEXT,
        duration_seconds         INTEGER,
        resumable_state_json     TEXT,
        batch_size               INTEGER NOT NULL DEFAULT 50,
        next_table_index         INTEGER NOT NULL DEFAULT 0,
        created_at               TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_table_profiles (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id       INTEGER NOT NULL,
        source_id                   INTEGER NOT NULL,
        table_fqn                   TEXT    NOT NULL,
        table_name                  TEXT    NOT NULL,
        schema_name                 TEXT    NOT NULL,
        table_type                  TEXT    NOT NULL DEFAULT 'TABLE',
        exact_row_count             INTEGER,
        estimated_row_count         INTEGER,
        row_count_tier              TEXT,
        has_date_column             INTEGER NOT NULL DEFAULT 0,
        date_column_name            TEXT,
        earliest_record             TEXT,
        latest_record               TEXT,
        data_span_days              INTEGER,
        data_currency               TEXT    NOT NULL DEFAULT 'UNKNOWN',
        column_count                INTEGER NOT NULL DEFAULT 0,
        pk_column_count             INTEGER NOT NULL DEFAULT 0,
        fk_count                    INTEGER NOT NULL DEFAULT 0,
        referenced_by_count         INTEGER NOT NULL DEFAULT 0,
        is_junction_table           INTEGER NOT NULL DEFAULT 0,
        is_root_table               INTEGER NOT NULL DEFAULT 0,
        is_leaf_table               INTEGER NOT NULL DEFAULT 0,
        has_identity_column         INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage         REAL,
        completeness_score          REAL,
        table_class                 TEXT,
        classification_confidence   REAL,
        classification_evidence_json TEXT,
        competing_classes_json      TEXT,
        classification_rule_version TEXT,
        pii_column_count            INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count         INTEGER NOT NULL DEFAULT 0,
        profiling_depth             TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms       INTEGER,
        profiling_status            TEXT    NOT NULL DEFAULT 'COMPLETE',
        skip_reason                 TEXT,
        profiled_at                 TEXT,
        created_at                  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at                  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_column_profiles (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id   INTEGER NOT NULL,
        source_id               INTEGER NOT NULL,
        table_fqn               TEXT    NOT NULL,
        column_name             TEXT    NOT NULL,
        data_type               TEXT    NOT NULL,
        raw_type                TEXT,
        is_nullable             INTEGER NOT NULL DEFAULT 1,
        is_primary_key          INTEGER NOT NULL DEFAULT 0,
        is_identity             INTEGER NOT NULL DEFAULT 0,
        ordinal_position        INTEGER NOT NULL DEFAULT 0,
        null_count              INTEGER,
        null_percentage         REAL,
        populated_count         INTEGER,
        populated_percentage    REAL,
        empty_string_count      INTEGER,
        zero_count              INTEGER,
        distinct_count          INTEGER,
        distinct_percentage     REAL,
        uniqueness_score        REAL,
        cardinality_tier        TEXT,
        min_value               TEXT,
        max_value               TEXT,
        min_length              INTEGER,
        max_length_observed     INTEGER,
        avg_length              REAL,
        mean_value              REAL,
        std_deviation           REAL,
        p5_value                TEXT,
        p95_value               TEXT,
        dominant_pattern        TEXT,
        pattern_coverage        REAL,
        email_match_rate        REAL,
        phone_match_rate        REAL,
        guid_match_rate         REAL,
        date_string_rate        REAL,
        numeric_string_rate     REAL,
        masked_value_rate       REAL,
        semantic_type           TEXT,
        semantic_confidence     REAL,
        semantic_evidence_json  TEXT,
        semantic_rule_version   TEXT,
        pii_name_heuristic      INTEGER NOT NULL DEFAULT 0,
        pii_confirmed           INTEGER NOT NULL DEFAULT 0,
        pii_signals_json        TEXT,
        top_values_coverage     REAL,
        profiling_depth         TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms   INTEGER,
        profiling_status        TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
"""


# ── In-memory DB fixture ───────────────────────────────────────────────────────

class _NoClose:
    """Wraps sqlite3.Connection and suppresses close() so the in-memory DB
    survives across multiple service calls within a single test."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


@pytest.fixture()
def db():
    """Fresh in-memory DB.  Patches data.profiling_service.get_connection."""
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.profiling_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ── Schema-building helpers ────────────────────────────────────────────────────

def _col(name: str, pos: int) -> ColumnInfo:
    return ColumnInfo(
        column_name=name,
        ordinal_position=pos,
        data_type="TEXT",
        raw_type="nvarchar",
        is_nullable=True,
        is_primary_key=False,
        is_identity=False,
    )


def _table(schema: str, name: str, cols: list[ColumnInfo]) -> TableInfo:
    return TableInfo(
        table_name=name,
        schema_name=schema,
        table_fqn=f"{schema}.{name}",
        table_type="TABLE",
        columns=cols,
    )


def _snapshot(source_id: int, tables: list[TableInfo]) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id,
        source_type="mssql",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )


def _seed_source(db: sqlite3.Connection, user_id: str = "user-1") -> int:
    cur = db.execute(
        "INSERT INTO data_source_connections (user_id) VALUES (?)", (user_id,)
    )
    db.commit()
    return cur.lastrowid


def _seed_schema_snapshot(
    db: sqlite3.Connection, source_id: int, snapshot: SchemaSnapshot
) -> int:
    cur = db.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, "
        "column_count, snapshot_json, discovered_at, created_at) "
        "VALUES (?, 1, 'mssql', ?, 0, ?, ?, ?, CURRENT_TIMESTAMP)",
        (
            source_id,
            snapshot.table_count,
            snapshot.column_count,
            dataclasses.asdict(snapshot).__class__.__name__,  # placeholder; overwritten below
            snapshot.discovered_at,
        ),
    )
    ss_id = cur.lastrowid
    db.execute(
        "UPDATE schema_snapshots SET snapshot_json = ? WHERE id = ?",
        (json.dumps(dataclasses.asdict(snapshot)), ss_id),
    )
    db.commit()
    return ss_id


# ── Test 1: start_batch_profiling seeds columns_total correctly ───────────────

def test_start_batch_profiling_seeds_columns_total(db):
    """After start_batch_profiling, columns_total must equal the total number
    of columns in all non-excluded tables in the schema snapshot."""
    source_id = _seed_source(db)

    tables = [
        _table("dbo", "students",  [_col("id", 1), _col("name", 2), _col("email", 3)]),  # 3 cols
        _table("dbo", "courses",   [_col("id", 1), _col("title", 2)]),                    # 2 cols
        _table("dbo", "enrollments", [_col("id", 1), _col("student_id", 2),
                                      _col("course_id", 3), _col("grade", 4)]),            # 4 cols
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1")

    assert state is not None

    row = db.execute(
        "SELECT columns_total FROM profiling_snapshots WHERE id = ?",
        (state.profiling_snapshot_id,),
    ).fetchone()

    assert row is not None
    # 3 + 2 + 4 = 9 columns total
    assert row["columns_total"] == 9


# ── Test 2: columns_total reflects exclusions ─────────────────────────────────

def test_start_batch_profiling_respects_exclusions(db):
    """Tables whose names start with excluded prefixes must not be counted."""
    source_id = _seed_source(db)

    tables = [
        _table("dbo", "students",     [_col("id", 1), _col("name", 2)]),         # 2 cols — included
        _table("dbo", "tmp_import",   [_col("id", 1), _col("raw", 2), _col("x", 3)]),  # excluded prefix
        _table("dbo", "stg_staging",  [_col("col1", 1)]),                         # excluded prefix
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1")

    assert state is not None

    row = db.execute(
        "SELECT columns_total, tables_total FROM profiling_snapshots WHERE id = ?",
        (state.profiling_snapshot_id,),
    ).fetchone()

    assert row is not None
    # Only 'students' passes the default excluded_prefixes filter
    assert row["tables_total"] == 1
    assert row["columns_total"] == 2


# ── Test 3: completed batch snapshot has columns_total > 0 ───────────────────

def test_completed_batch_snapshot_columns_total_nonzero(db):
    """A profiling snapshot that reaches status=COMPLETE must have columns_total > 0
    when the source schema contains real columns."""
    source_id = _seed_source(db)

    tables = [
        _table("dbo", "orders", [_col("order_id", 1), _col("amount", 2), _col("created_at", 3)]),
        _table("dbo", "items",  [_col("item_id", 1), _col("name", 2)]),
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1")

    assert state is not None

    row = db.execute(
        "SELECT columns_total, status FROM profiling_snapshots WHERE id = ?",
        (state.profiling_snapshot_id,),
    ).fetchone()

    assert row["columns_total"] > 0
    # Snapshot starts RUNNING; columns_total is correct immediately.
    assert row["status"] == ProfilingStatus.RUNNING.value


# ── Test 4: structural profiling path is unchanged ───────────────────────────

def test_structural_profiling_columns_total_set_by_engine(db):
    """run_structural_profiling must continue to write the correct columns_total
    via _SNAP_INSERT.  This path is independent of the batch profiling changes."""
    source_id = _seed_source(db)

    tables = [
        _table("dbo", "products", [_col("product_id", 1), _col("sku", 2),
                                    _col("name", 3), _col("price", 4)]),  # 4 cols
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    result = run_structural_profiling(source_id, "user-1")

    assert result is not None

    row = db.execute(
        "SELECT columns_total, status FROM profiling_snapshots "
        "WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()

    assert row is not None
    assert row["columns_total"] == 4
    assert row["status"] == ProfilingStatus.COMPLETE.value


# ── Test 5: get_latest_profile returns the correct columns_total ──────────────

def test_get_latest_profile_returns_correct_columns_total(db):
    """The GET /v1/sources/{id}/profile service function must return the
    columns_total value written by start_batch_profiling (after Fix 1)."""
    source_id = _seed_source(db)

    tables = [
        _table("dbo", "customers", [_col("id", 1), _col("name", 2),
                                     _col("email", 3), _col("phone", 4),
                                     _col("created_at", 5)]),  # 5 cols
        _table("dbo", "addresses", [_col("id", 1), _col("line1", 2),
                                     _col("city", 3)]),          # 3 cols
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    # Trigger batch profiling (Fix 1 seeds columns_total = 8)
    state = start_batch_profiling(source_id, "user-1")
    assert state is not None

    # Manually mark the snapshot COMPLETE so get_latest_profile can return it
    db.execute(
        "UPDATE profiling_snapshots SET status = 'COMPLETE' WHERE id = ?",
        (state.profiling_snapshot_id,),
    )
    db.commit()

    result = get_latest_profile(source_id, "user-1")

    assert result is not None
    snapshot = result["snapshot"]
    # 5 + 3 = 8 columns total
    assert snapshot["columns_total"] == 8
    assert snapshot["status"] == "COMPLETE"


# ── Repair SQL (mirrors data/models.py migration exactly) ─────────────────────

_REPAIR_SQL = """
    UPDATE profiling_snapshots
    SET columns_total = (
        SELECT COALESCE(SUM(column_count), 0)
        FROM profiling_table_profiles
        WHERE profiling_table_profiles.profiling_snapshot_id = profiling_snapshots.id
    )
    WHERE columns_total = 0
    AND EXISTS (
        SELECT 1
        FROM profiling_table_profiles
        WHERE profiling_table_profiles.profiling_snapshot_id = profiling_snapshots.id
    )
"""


# ── Test 6: repair SQL fixes rows that have stored table profiles ──────────────

def test_repair_updates_columns_total_from_table_profiles(db):
    """A profiling_snapshots row with columns_total=0 and existing
    profiling_table_profiles rows must have columns_total set to the sum of
    column_count values after the migration repair runs."""
    source_id = _seed_source(db)

    # Insert a profiling snapshot with columns_total explicitly 0 (the bug state).
    snap_id = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, columns_total) "
        "VALUES (?, 1, 1, 'full', 'COMPLETE', 0)",
        (source_id,),
    ).lastrowid
    db.commit()

    # Insert three table profiles with known column counts: 5 + 3 + 7 = 15.
    for fqn, col_count in [("dbo.customers", 5), ("dbo.orders", 3), ("dbo.products", 7)]:
        name = fqn.split(".")[1]
        db.execute(
            "INSERT INTO profiling_table_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
            "table_type, column_count, profiling_depth, profiling_status, created_at, updated_at) "
            "VALUES (?,?,?,?,'dbo','TABLE',?,'STRUCTURAL_ONLY','COMPLETE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
            (snap_id, source_id, fqn, name, col_count),
        )
    db.commit()

    # Confirm pre-repair state.
    pre = db.execute(
        "SELECT columns_total FROM profiling_snapshots WHERE id = ?", (snap_id,)
    ).fetchone()
    assert pre["columns_total"] == 0

    # Run the repair.
    db.execute(_REPAIR_SQL)
    db.commit()

    # Post-repair: columns_total must equal 5 + 3 + 7 = 15.
    post = db.execute(
        "SELECT columns_total FROM profiling_snapshots WHERE id = ?", (snap_id,)
    ).fetchone()
    assert post["columns_total"] == 15


# ── Test 7: repair SQL leaves rows with no table profiles untouched ───────────

def test_repair_leaves_empty_snapshots_at_zero(db):
    """A profiling_snapshots row with columns_total=0 and no matching rows in
    profiling_table_profiles must remain 0 after the repair runs (nothing to
    derive from)."""
    source_id = _seed_source(db)

    # Snapshot with columns_total=0 and no table profiles at all.
    snap_no_profiles = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, columns_total) "
        "VALUES (?, 1, 1, 'full', 'RUNNING', 0)",
        (source_id,),
    ).lastrowid

    # Snapshot with a real columns_total that must not be overwritten.
    snap_already_correct = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, columns_total) "
        "VALUES (?, 1, 2, 'structural_only', 'COMPLETE', 42)",
        (source_id,),
    ).lastrowid
    db.commit()

    # Run the repair.
    db.execute(_REPAIR_SQL)
    db.commit()

    row_no_profiles = db.execute(
        "SELECT columns_total FROM profiling_snapshots WHERE id = ?", (snap_no_profiles,)
    ).fetchone()
    row_correct = db.execute(
        "SELECT columns_total FROM profiling_snapshots WHERE id = ?", (snap_already_correct,)
    ).fetchone()

    # Row with no table profiles stays 0 (no data to derive from).
    assert row_no_profiles["columns_total"] == 0
    # Row that was already non-zero is untouched (WHERE columns_total = 0 guard).
    assert row_correct["columns_total"] == 42
