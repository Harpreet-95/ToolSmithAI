"""
Tests for the Structural Coverage authoritative-count fix.

Bug: profiling_snapshots.tables_profiled is a hand-incremented counter
     (COALESCE(tables_profiled, 0) + tables_saved in continue_batch_profiling).
     tables_saved is len(batch_profiles) -- the batch size attempted, not the
     number of rows actually newly inserted. INSERT OR IGNORE correctly
     deduplicates rows in profiling_table_profiles (UNIQUE(profiling_snapshot_id,
     table_fqn)), but the counter has no idea a row was ignored as a duplicate.
     If a batch window is ever processed more than once (e.g. a browser-refresh
     race between the original polling loop and ProfilingJobContext.recoverActiveJob),
     the counter drifts above the true distinct-table count.

     Confirmed in production: CCPP SQL Server (source_id=1, snapshot id=9016) showed
     tables_profiled=1501 while profiling_table_profiles held exactly 1401 distinct
     rows (395 STATISTICAL + 1006 STRUCTURAL_ONLY), producing "107%" coverage.

Fix:   get_latest_profile now returns COUNT(DISTINCT table_fqn) FROM
       profiling_table_profiles as tables_profiled instead of the stored,
       drift-prone counter column. tables_total and the write path are untouched.

All tests use an in-memory SQLite database patched over data.db.get_connection
so they never touch the real toolsmith.db on disk.

Run from the project root:
    venv/Scripts/pytest tests/test_structural_coverage_authoritative_count.py -v
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
os.environ.setdefault("JWT_SECRET",     "test-jwt-structural-coverage-long-enough!!")
os.environ.setdefault("USER_ID_SALT",   "test-salt-structural-coverage")

from core.connectors.schema import ColumnInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.profiling.models import ProfilingStatus
from data.profiling_service import (
    continue_batch_profiling,
    get_latest_profile,
    start_batch_profiling,
)


# ── Minimal in-memory schema (mirrors data/models.py) ──────────────────────────

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
        cancel_requested         INTEGER NOT NULL DEFAULT 0,
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

    CREATE UNIQUE INDEX idx_prtp_snapshot_fqn
        ON profiling_table_profiles (profiling_snapshot_id, table_fqn);

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
        p25_value               TEXT,
        p50_value               TEXT,
        p75_value               TEXT,
        p95_value               TEXT,
        blank_percentage        REAL,
        histogram_json          TEXT,
        distribution_shape      TEXT,
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
        completeness_score      REAL,
        format_consistency_score REAL,
        valid_count             INTEGER,
        invalid_count           INTEGER,
        invalid_percentage      REAL,
        validation_status       TEXT,
        quality_score           REAL,
        quality_grade           TEXT,
        quality_summary_json    TEXT,
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


# ── Schema-building helpers (for the start/continue batch-profiling test) ──────

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
        "VALUES (?, 1, 'mssql', ?, 0, ?, '{}', ?, CURRENT_TIMESTAMP)",
        (
            source_id,
            snapshot.table_count,
            snapshot.column_count,
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


def _insert_table_profile(
    db: sqlite3.Connection,
    snapshot_id: int,
    source_id: int,
    table_fqn: str,
    profiling_depth: str = "STRUCTURAL_ONLY",
) -> None:
    table_name = table_fqn.split(".")[-1]
    db.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        "table_type, profiling_depth, profiling_status, created_at, updated_at) "
        "VALUES (?,?,?,?,'dbo','TABLE',?,'COMPLETE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (snapshot_id, source_id, table_fqn, table_name, profiling_depth),
    )


# ── Test A ──────────────────────────────────────────────────────────────────────

def test_get_latest_profile_returns_distinct_count_not_inflated_counter(db):
    """Regression test for the CCPP 107% Structural Coverage bug: the stored
    tables_profiled counter can drift above the real row count. get_latest_profile
    must always return COUNT(DISTINCT table_fqn), not the raw stored column.

    Scaled-down mirror of production snapshot id=9016
    (tables_total=1401, stored tables_profiled=1501, real distinct rows=1401)."""
    source_id = _seed_source(db)

    snap_id = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, "
        "tables_total, tables_profiled) VALUES (?, 1, 1, 'full', 'COMPLETE', 5, 9)",
        (source_id,),
    ).lastrowid
    db.commit()

    for i in range(5):
        _insert_table_profile(db, snap_id, source_id, f"dbo.t{i}")
    db.commit()

    result = get_latest_profile(source_id, "user-1")
    assert result is not None
    assert result["snapshot"]["tables_profiled"] == 5, (
        "Expected COUNT(DISTINCT table_fqn)=5, not the inflated stored counter (9)."
    )


# ── Test B ──────────────────────────────────────────────────────────────────────

def test_duplicate_batch_processing_does_not_inflate_returned_count(db):
    """Simulates the real-world race: a duplicate/racing continue_batch_profiling
    call re-processes the same batch window (e.g. ProfilingJobContext.recoverActiveJob
    starting a second polling loop after a browser refresh while the prior in-flight
    request is still committing). INSERT OR IGNORE keeps the row count correct, but
    the stored counter still doubles. get_latest_profile must not inherit that."""
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(5)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=5, mode="STRUCTURAL_ONLY")
    assert state is not None
    snap_id = state.profiling_snapshot_id

    # First pass processes all 5 tables and completes the snapshot.
    first = continue_batch_profiling(source_id, "user-1", snap_id)
    assert first.status == ProfilingStatus.COMPLETE

    # Simulate a duplicate/racing continue call for the same batch window by
    # resetting next_table_index and status, as if a second in-flight request
    # read the pre-update snapshot state before the first call's commit landed.
    db.execute(
        "UPDATE profiling_snapshots SET next_table_index = 0, status = ? WHERE id = ?",
        (ProfilingStatus.RUNNING.value, snap_id),
    )
    db.commit()
    continue_batch_profiling(source_id, "user-1", snap_id)

    # Sanity check: the stored counter is now inflated (doubled), proving the
    # write-path race still exists and was NOT silently patched by this fix.
    raw = db.execute(
        "SELECT tables_profiled FROM profiling_snapshots WHERE id = ?", (snap_id,)
    ).fetchone()
    assert raw["tables_profiled"] == 10, (
        "setup sanity check failed: expected the stored counter to double to 10"
    )

    # But the row-level table_fqn set has exactly 5 distinct entries (idempotent
    # INSERT OR IGNORE), so the *returned* count must still be 5.
    result = get_latest_profile(source_id, "user-1")
    assert result["snapshot"]["tables_profiled"] == 5


# ── Test C ──────────────────────────────────────────────────────────────────────

def test_statistical_plus_structural_equals_returned_tables_profiled(db):
    """Invariant: statistical_count + structural_only_count == returned tables_profiled."""
    source_id = _seed_source(db)
    snap_id = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, "
        "tables_total, tables_profiled) VALUES (?, 1, 1, 'full', 'COMPLETE', 7, 999)",
        (source_id,),
    ).lastrowid
    db.commit()

    depths = ["STATISTICAL"] * 3 + ["STRUCTURAL_ONLY"] * 4
    for i, depth in enumerate(depths):
        _insert_table_profile(db, snap_id, source_id, f"dbo.t{i}", profiling_depth=depth)
    db.commit()

    result = get_latest_profile(source_id, "user-1")
    tables = result["tables"]
    statistical_count = sum(1 for t in tables if t["profiling_depth"] == "STATISTICAL")
    structural_count  = sum(1 for t in tables if t["profiling_depth"] == "STRUCTURAL_ONLY")

    assert statistical_count == 3
    assert structural_count == 4
    assert statistical_count + structural_count == result["snapshot"]["tables_profiled"]
    assert result["snapshot"]["tables_profiled"] == 7  # not the inflated stored 999


# ── Test D ──────────────────────────────────────────────────────────────────────

def test_returned_tables_profiled_never_exceeds_tables_total(db):
    """For a completed snapshot: returned tables_profiled <= tables_total,
    even when the stored counter is wildly inflated beyond tables_total."""
    source_id = _seed_source(db)
    snap_id = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, "
        "tables_total, tables_profiled) VALUES (?, 1, 1, 'full', 'COMPLETE', 4, 4000)",
        (source_id,),
    ).lastrowid
    db.commit()

    for i in range(4):
        _insert_table_profile(db, snap_id, source_id, f"dbo.t{i}")
    db.commit()

    result = get_latest_profile(source_id, "user-1")
    assert result["snapshot"]["tables_profiled"] == 4
    assert result["snapshot"]["tables_total"] == 4
    assert result["snapshot"]["tables_profiled"] <= result["snapshot"]["tables_total"]


# ── Test E ──────────────────────────────────────────────────────────────────────

def test_ownership_scoping_still_enforced(db):
    """get_latest_profile must continue to return None for a non-owning user_id,
    and the correct (fixed) data for the actual owner."""
    source_id = _seed_source(db, user_id="owner-user")
    db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, mode, status, "
        "tables_total, tables_profiled) VALUES (?, 1, 1, 'full', 'COMPLETE', 1, 1)",
        (source_id,),
    )
    db.commit()

    assert get_latest_profile(source_id, "someone-else") is None

    result_owner = get_latest_profile(source_id, "owner-user")
    assert result_owner is not None
    assert result_owner["snapshot"]["tables_profiled"] == 0  # no table rows inserted
