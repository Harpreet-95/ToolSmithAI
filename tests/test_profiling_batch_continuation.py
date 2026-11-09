"""
Tests for automatic profiling batch continuation (coverage milestone).

Root cause traced: a real production database profiling stalled at a small fraction of its tables because
(1) a fresh start_batch_profiling call always began a new snapshot at
    next_table_index=0, discarding any prior CANCELLED attempt's progress;
(2) continue_batch_profiling's per-table loop had no fault isolation — one
    unhandled exception anywhere in a batch discarded the whole batch and
    left the snapshot stuck RUNNING; and
(3) nothing drove continue_batch_profiling to completion without a browser
    tab polling it.
A fourth, independently-discovered bug: continue_batch_profiling never
called _classify_table for non-statistical (structural-only) tables in a
batch, leaving table_class permanently NULL for them regardless of whether
their name matched the classifier's vocabulary.

This file tests the fixes:
  A. Cross-snapshot resume in start_batch_profiling.
  B. Per-table fault isolation + classify_table-for-structural-tables in
     continue_batch_profiling.
  C. advance_batch_profiling_for_source / continue_all_active_profiling_batches
     driving a batch job to completion without external polling.

All tests use an in-memory SQLite database patched over data.db.get_connection
so they never touch the real toolsmith.db on disk.

Run from the project root:
    venv/Scripts/pytest tests/test_profiling_batch_continuation.py -v
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
os.environ.setdefault("JWT_SECRET",     "test-jwt-batch-continuation-long-enough!!")
os.environ.setdefault("USER_ID_SALT",   "test-salt-batch-continuation")

from core.connectors.schema import ColumnInfo, ForeignKeyInfo, SchemaInfo, SchemaSnapshot, TableInfo
from core.profiling.models import ProfilingStatus
from core.secrets.manager import get_secret_manager
from data.profiling_service import (
    advance_batch_profiling_for_source,
    cancel_batch_profiling,
    continue_all_active_profiling_batches,
    continue_batch_profiling,
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

    CREATE TABLE profiling_column_profiles (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id   INTEGER NOT NULL,
        source_id               INTEGER NOT NULL,
        table_fqn               TEXT    NOT NULL,
        column_name             TEXT    NOT NULL,
        data_type                TEXT    NOT NULL,
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
        avg_length               REAL,
        mean_value               REAL,
        std_deviation             REAL,
        p5_value                TEXT,
        p25_value                TEXT,
        p50_value                TEXT,
        p75_value                TEXT,
        p95_value                TEXT,
        blank_percentage         REAL,
        histogram_json           TEXT,
        distribution_shape       TEXT,
        dominant_pattern          TEXT,
        pattern_coverage          REAL,
        email_match_rate          REAL,
        phone_match_rate          REAL,
        guid_match_rate           REAL,
        date_string_rate          REAL,
        numeric_string_rate       REAL,
        masked_value_rate         REAL,
        semantic_type             TEXT,
        semantic_confidence       REAL,
        semantic_evidence_json    TEXT,
        semantic_rule_version     TEXT,
        pii_name_heuristic        INTEGER NOT NULL DEFAULT 0,
        pii_confirmed             INTEGER NOT NULL DEFAULT 0,
        pii_signals_json          TEXT,
        top_values_coverage       REAL,
        completeness_score        REAL,
        format_consistency_score  REAL,
        valid_count               INTEGER,
        invalid_count             INTEGER,
        invalid_percentage        REAL,
        validation_status         TEXT,
        quality_score             REAL,
        quality_grade             TEXT,
        quality_summary_json      TEXT,
        profiling_depth           TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms     INTEGER,
        profiling_status          TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    """Fresh in-memory DB. Patches data.profiling_service.get_connection."""
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


def _table(schema: str, name: str, cols: list[ColumnInfo], fks: list[ForeignKeyInfo] | None = None) -> TableInfo:
    return TableInfo(
        table_name=name,
        schema_name=schema,
        table_fqn=f"{schema}.{name}",
        table_type="TABLE",
        columns=cols,
        foreign_keys=fks or [],
    )


def _snapshot(source_id: int, tables: list[TableInfo]) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id,
        source_type="mssql",
        discovered_at=datetime.now(timezone.utc).isoformat(),
        schemas=[SchemaInfo(schema_name="dbo", tables=tables)],
    )


def _seed_source(db: sqlite3.Connection, user_id: str = "user-1", encrypted_config: str | None = None) -> int:
    cfg = encrypted_config or get_secret_manager().encrypt_secret(
        json.dumps({"host": "x", "database": "y", "username": "u", "password": "p"})
    )
    cur = db.execute(
        "INSERT INTO data_source_connections (user_id, encrypted_config_json) VALUES (?, ?)",
        (user_id, cfg),
    )
    db.commit()
    return cur.lastrowid


def _seed_schema_snapshot(db: sqlite3.Connection, source_id: int, snapshot: SchemaSnapshot, version: int = 1) -> int:
    cur = db.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, "
        "column_count, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, 'mssql', ?, 0, ?, ?, ?, CURRENT_TIMESTAMP)",
        (
            source_id, version,
            snapshot.table_count,
            snapshot.column_count,
            json.dumps(dataclasses.asdict(snapshot)),
            snapshot.discovered_at,
        ),
    )
    ss_id = cur.lastrowid
    db.commit()
    return ss_id


class _FakeConn:
    def close(self):
        pass


class _FakeConnector:
    def get_query_builder(self):
        return object()

    def open_connection(self, config):
        return _FakeConn()


# =============================================================================
# Fix A — cross-snapshot resume
# =============================================================================

def test_resume_after_cancel_preserves_progress(db):
    """A CANCELLED batch snapshot with partial progress is resumed in place by
    a later start_batch_profiling call — not discarded and restarted at 0."""
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(10)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=3, mode="STRUCTURAL_ONLY")
    original_id = state.profiling_snapshot_id

    state = continue_batch_profiling(source_id, "user-1", original_id)
    assert state.next_table_index == 3
    assert state.status == ProfilingStatus.RUNNING

    cancel_batch_profiling(source_id, "user-1", original_id)
    state = continue_batch_profiling(source_id, "user-1", original_id)
    assert state.status == ProfilingStatus.CANCELLED

    resumed = start_batch_profiling(source_id, "user-1", batch_size=3, mode="STRUCTURAL_ONLY")

    assert resumed.profiling_snapshot_id == original_id, (
        "Resume must reuse the existing snapshot row, not create a new one — "
        "otherwise the 3 already-profiled tables' progress is discarded."
    )
    assert resumed.next_table_index == 3
    assert resumed.status == ProfilingStatus.RUNNING

    row = db.execute(
        "SELECT status, cancel_requested FROM profiling_snapshots WHERE id = ?", (original_id,)
    ).fetchone()
    assert row["status"] == ProfilingStatus.RUNNING.value
    assert row["cancel_requested"] == 0


def test_resume_reaches_full_coverage_across_multiple_restarts(db):
    """Simulates the real-world stall pattern: several separate start/continue
    attempts, each abandoned partway, must still add up to full coverage
    instead of each restarting the walk from table 0."""
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(9)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    # Attempt 1: profile 3, then abandon (cancel).
    state = start_batch_profiling(source_id, "user-1", batch_size=3, mode="STRUCTURAL_ONLY")
    snap_id = state.profiling_snapshot_id
    continue_batch_profiling(source_id, "user-1", snap_id)
    cancel_batch_profiling(source_id, "user-1", snap_id)
    continue_batch_profiling(source_id, "user-1", snap_id)  # flips to CANCELLED

    # Attempt 2: resume, profile 3 more (tables 3-5), abandon again.
    state = start_batch_profiling(source_id, "user-1", batch_size=3, mode="STRUCTURAL_ONLY")
    assert state.profiling_snapshot_id == snap_id
    continue_batch_profiling(source_id, "user-1", snap_id)
    cancel_batch_profiling(source_id, "user-1", snap_id)
    continue_batch_profiling(source_id, "user-1", snap_id)

    # Attempt 3: resume and finish.
    state = start_batch_profiling(source_id, "user-1", batch_size=3, mode="STRUCTURAL_ONLY")
    assert state.profiling_snapshot_id == snap_id
    final = continue_batch_profiling(source_id, "user-1", snap_id)

    assert final.status == ProfilingStatus.COMPLETE
    assert final.next_table_index == 9

    distinct = db.execute(
        "SELECT COUNT(DISTINCT table_fqn) FROM profiling_table_profiles WHERE profiling_snapshot_id = ?",
        (snap_id,),
    ).fetchone()[0]
    assert distinct == 9, "All 9 tables must have been profiled across the three attempts, no duplicates lost."


def test_resume_does_not_apply_when_snapshot_complete(db):
    """A COMPLETE snapshot must never be resumed — start_batch_profiling
    creates a fresh snapshot instead."""
    source_id = _seed_source(db)
    tables = [_table("dbo", "t1", [_col("id", 1)])]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=50, mode="STRUCTURAL_ONLY")
    completed = continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)
    assert completed.status == ProfilingStatus.COMPLETE

    new_state = start_batch_profiling(source_id, "user-1", batch_size=50, mode="STRUCTURAL_ONLY")
    assert new_state.profiling_snapshot_id != state.profiling_snapshot_id
    assert new_state.next_table_index == 0


def test_resume_does_not_cross_schema_versions(db):
    """A CANCELLED snapshot bound to an old schema_snapshot_id must not be
    resumed once a newer schema snapshot exists — the old sorted_fqns plan
    may no longer match the current schema."""
    source_id = _seed_source(db)
    tables_v1 = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(5)]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables_v1), version=1)

    state = start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")
    old_id = state.profiling_snapshot_id
    continue_batch_profiling(source_id, "user-1", old_id)
    cancel_batch_profiling(source_id, "user-1", old_id)
    continue_batch_profiling(source_id, "user-1", old_id)

    # Schema rediscovered — new (higher-version) schema_snapshots row.
    tables_v2 = tables_v1 + [_table("dbo", "t_new", [_col("id", 1)])]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables_v2), version=2)

    new_state = start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")
    assert new_state.profiling_snapshot_id != old_id
    assert new_state.next_table_index == 0
    assert new_state.total_tables == 6


# =============================================================================
# Fix B — per-table fault isolation + classification for structural tables
# =============================================================================

def test_one_table_failure_does_not_abort_batch(db):
    source_id = _seed_source(db)
    tables = [
        _table("dbo", "good_a", [_col("id", 1)]),
        _table("dbo", "poison", [_col("id", 1)]),
        _table("dbo", "good_b", [_col("id", 1)]),
    ]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=10, mode="FULL")

    def _boom(conn, tp, config, builder):
        if tp.table_fqn == "dbo.poison":
            raise RuntimeError("simulated hang/error on this table")

    with patch("data.profiling_service.get_connector", return_value=_FakeConnector), \
         patch("data.profiling_service._run_statistical_pass", side_effect=_boom):
        result = continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)

    assert result.status == ProfilingStatus.COMPLETE, (
        "One table raising must not stop the batch or leave the snapshot stuck RUNNING."
    )
    assert result.next_table_index == 3

    rows = {
        r["table_fqn"]: r for r in db.execute(
            "SELECT table_fqn, profiling_status, skip_reason FROM profiling_table_profiles "
            "WHERE profiling_snapshot_id = ?",
            (state.profiling_snapshot_id,),
        ).fetchall()
    }
    assert set(rows) == {"dbo.good_a", "dbo.poison", "dbo.good_b"}, "All 3 tables must still be saved."
    assert rows["dbo.poison"]["profiling_status"] == "FAILED"
    assert "simulated hang/error" in rows["dbo.poison"]["skip_reason"]
    assert rows["dbo.good_a"]["profiling_status"] == "COMPLETE"
    assert rows["dbo.good_b"]["profiling_status"] == "COMPLETE"

    failed_count = db.execute(
        "SELECT tables_failed FROM profiling_snapshots WHERE id = ?",
        (state.profiling_snapshot_id,),
    ).fetchone()[0]
    assert failed_count == 1


def test_structural_only_tables_are_classified_in_batch_path(db):
    """Regression for a second bug found alongside the fault-isolation fix:
    continue_batch_profiling previously only called _classify_table for
    tables that received live statistical profiling — structural-only tables
    (the majority in a mode=STRUCTURAL_ONLY run, or any table that didn't
    make the statistical cut) were persisted with table_class permanently
    NULL, never even reaching 'Unknown'."""
    source_id = _seed_source(db)
    # 'audit_log' matches _AUDIT_TOKENS in table_classifier.py with no
    # additional structural gating required, so classification must succeed
    # from name evidence alone even with zero live statistics.
    tables = [_table("dbo", "audit_log", [_col("id", 1), _col("changed_by", 2)])]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=10, mode="STRUCTURAL_ONLY")
    result = continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)
    assert result.status == ProfilingStatus.COMPLETE

    row = db.execute(
        "SELECT table_class, classification_confidence FROM profiling_table_profiles "
        "WHERE profiling_snapshot_id = ? AND table_fqn = ?",
        (state.profiling_snapshot_id, "dbo.audit_log"),
    ).fetchone()
    assert row["table_class"] is not None, (
        "table_class is NULL — classify_table never ran for this structural-only table."
    )
    assert row["table_class"] == "Audit"
    assert row["classification_confidence"] is not None


# =============================================================================
# Fix C — automatic continuation driver
# =============================================================================

def test_advance_batch_profiling_for_source_drives_to_completion(db):
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(7)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")

    final = advance_batch_profiling_for_source(
        source_id, "user-1", state.profiling_snapshot_id, time_budget_s=10.0,
    )

    assert final is not None
    assert final.status == ProfilingStatus.COMPLETE
    assert final.next_table_index == 7
    assert final.completed_tables == 7


def test_advance_batch_profiling_for_source_resolves_active_snapshot(db):
    """When profiling_snapshot_id is omitted, the active RUNNING/PENDING
    snapshot for the source is found automatically."""
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(4)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")

    final = advance_batch_profiling_for_source(source_id, "user-1", time_budget_s=10.0)
    assert final is not None
    assert final.status == ProfilingStatus.COMPLETE


def test_advance_batch_profiling_for_source_returns_none_when_nothing_active(db):
    source_id = _seed_source(db)
    result = advance_batch_profiling_for_source(source_id, "user-1", time_budget_s=1.0)
    assert result is None


def test_continue_all_active_profiling_batches_advances_running_source(db):
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(5)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")

    summaries = continue_all_active_profiling_batches(time_budget_s=10.0)

    assert len(summaries) == 1
    assert summaries[0]["source_id"] == source_id
    assert summaries[0]["profiling_snapshot_id"] == state.profiling_snapshot_id
    assert summaries[0]["status"] == "COMPLETE"
    assert summaries[0]["next_table_index"] == 5


def test_continue_all_active_profiling_batches_resumes_cancelled_source(db):
    source_id = _seed_source(db)
    tables = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(6)]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=2, mode="STRUCTURAL_ONLY")
    continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)  # -> index 2
    cancel_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)
    continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)  # -> CANCELLED

    cancelled_row = db.execute(
        "SELECT status, next_table_index FROM profiling_snapshots WHERE id = ?",
        (state.profiling_snapshot_id,),
    ).fetchone()
    assert cancelled_row["status"] == ProfilingStatus.CANCELLED.value
    assert cancelled_row["next_table_index"] == 2

    summaries = continue_all_active_profiling_batches(time_budget_s=10.0)

    assert len(summaries) == 1
    assert summaries[0]["profiling_snapshot_id"] == state.profiling_snapshot_id, (
        "The CANCELLED snapshot must be resumed in place, not replaced."
    )
    assert summaries[0]["status"] == "COMPLETE"
    assert summaries[0]["next_table_index"] == 6


def test_continue_all_active_profiling_batches_skips_complete_sources(db):
    source_id = _seed_source(db)
    tables = [_table("dbo", "t1", [_col("id", 1)])]
    snap = _snapshot(source_id, tables)
    _seed_schema_snapshot(db, source_id, snap)

    state = start_batch_profiling(source_id, "user-1", batch_size=50, mode="STRUCTURAL_ONLY")
    continue_batch_profiling(source_id, "user-1", state.profiling_snapshot_id)

    summaries = continue_all_active_profiling_batches(time_budget_s=5.0)
    assert summaries == []


def test_continue_all_active_profiling_batches_only_touches_latest_snapshot_per_source(db):
    """A COMPLETE snapshot must not be shadowed / regressed by resuming an
    older CANCELLED attempt sitting behind it — preserves the status-aware
    snapshot selection rule (profiling_snapshot_resolver) under batch
    continuation. Two distinct profiling_snapshots rows are created here (a
    schema rediscovery invalidates the resume match, per Fix A), so this is a
    genuine two-row latest-wins scenario, not the same row being reused."""
    source_id = _seed_source(db)
    tables_v1 = [_table("dbo", f"t{i}", [_col("id", 1)]) for i in range(4)]
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables_v1), version=1)

    # Older attempt (schema v1): partially profiled then cancelled.
    old_state = start_batch_profiling(source_id, "user-1", batch_size=1, mode="STRUCTURAL_ONLY")
    continue_batch_profiling(source_id, "user-1", old_state.profiling_snapshot_id)
    cancel_batch_profiling(source_id, "user-1", old_state.profiling_snapshot_id)
    continue_batch_profiling(source_id, "user-1", old_state.profiling_snapshot_id)  # CANCELLED, version 1

    # Schema rediscovered — new schema_snapshots row invalidates the resume match.
    _seed_schema_snapshot(db, source_id, _snapshot(source_id, tables_v1), version=2)

    # Newer attempt (schema v2): a fresh snapshot row that completes fully.
    new_state = start_batch_profiling(source_id, "user-1", batch_size=50, mode="STRUCTURAL_ONLY")
    assert new_state.profiling_snapshot_id != old_state.profiling_snapshot_id
    final = continue_batch_profiling(source_id, "user-1", new_state.profiling_snapshot_id)
    assert final.status == ProfilingStatus.COMPLETE

    summaries = continue_all_active_profiling_batches(time_budget_s=5.0)
    assert summaries == [], (
        "The latest snapshot for this source is COMPLETE — the older CANCELLED "
        "row behind it must not be resurrected."
    )
