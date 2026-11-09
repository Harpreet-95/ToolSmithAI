"""
Tests for data.profiling_snapshot_resolver — Phase 1 of the Authoritative
Profiling Snapshot Architecture (status-aware selection).

Phase 0 (superseded) reproduced the platform's original status-independent
"highest snapshot_version" selection verbatim. Phase 1 introduces the
authority-aware policy Phase 0's own docstring flagged as deferred: prefer
the newest COMPLETE snapshot; fall back to the newest snapshot of any status
only when the source has no COMPLETE snapshot at all. Confirmed live against
a real production database (source_id=1): the newest snapshot was CANCELLED after profiling
only a fraction of its tables, silently starving most tables (including
dbo.EDU_Track) of all profiling evidence — schema/profiling context came back
None for every one of them — even though a COMPLETE snapshot one version
older had full, valid data for every table.

Run from the project root:
    venv/Scripts/pytest tests/test_profiling_snapshot_resolver.py -v
"""
import sqlite3
from unittest.mock import patch

import pytest

from data.profiling_snapshot_resolver import (
    ProfilingSnapshotDetail,
    ProfilingSnapshotRef,
    get_latest_profiling_snapshot,
    get_latest_profiling_snapshot_detail,
)

_SCHEMA = """
    CREATE TABLE profiling_snapshots (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id            INTEGER NOT NULL,
        schema_snapshot_id   INTEGER NOT NULL DEFAULT 1,
        snapshot_version     INTEGER NOT NULL,
        status               TEXT    NOT NULL,
        mode                 TEXT    NOT NULL DEFAULT 'full',
        created_at           TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at         TEXT,
        tables_total         INTEGER NOT NULL DEFAULT 0,
        tables_profiled      INTEGER NOT NULL DEFAULT 0,
        columns_profiled     INTEGER NOT NULL DEFAULT 0,
        pii_columns_found    INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled  INTEGER NOT NULL DEFAULT 0
    );
"""


class _NoClose:
    """Wraps sqlite3.Connection and suppresses close() so the in-memory DB
    survives across multiple get_connection() calls within a single test —
    mirrors the existing convention in tests/test_batch_profiling_columns_total.py."""

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
    """Fresh in-memory DB. Patches data.profiling_snapshot_resolver.get_connection."""
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.profiling_snapshot_resolver.get_connection", return_value=wrapper):
        yield conn
    conn.close()


def _insert_snapshot(
    db: sqlite3.Connection,
    source_id: int,
    snapshot_version: int,
    status: str,
    schema_snapshot_id: int = 1,
) -> int:
    cur = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, status) "
        "VALUES (?, ?, ?, ?)",
        (source_id, schema_snapshot_id, snapshot_version, status),
    )
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# 1. Highest snapshot_version is selected
# ---------------------------------------------------------------------------

def test_selects_highest_snapshot_version(db):
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    newest_id = _insert_snapshot(db, source_id=1, snapshot_version=2, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")  # different source, irrelevant

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == newest_id
    assert result.snapshot_version == 2


# ---------------------------------------------------------------------------
# 2. Older COMPLETE beats newer CANCELLED/FAILED/RUNNING/PENDING — the core
# Phase 1 policy change. A newer bad/incomplete snapshot must never shadow
# older, usable COMPLETE evidence.
# ---------------------------------------------------------------------------

def test_older_complete_beats_newer_cancelled(db):
    complete_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


def test_older_complete_beats_newer_failed(db):
    complete_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=2, status="FAILED")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


def test_older_complete_beats_newer_running(db):
    complete_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


def test_multiple_complete_snapshots_newest_complete_wins(db):
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")
    newest_complete_id = _insert_snapshot(db, source_id=1, snapshot_version=3, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=4, status="RUNNING")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == newest_complete_id
    assert result.snapshot_version == 3
    assert result.status == "COMPLETE"


def test_no_complete_snapshot_falls_back_to_newest_any_status(db):
    """A source with NO COMPLETE snapshot yet (e.g. its first profiling run
    is still RUNNING) must still get that run's data rather than newly
    regressing to None — degrades exactly as the pre-Phase-1 platform did
    for this one case."""
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="CANCELLED")
    running_id = _insert_snapshot(db, source_id=1, snapshot_version=2, status="RUNNING")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == running_id
    assert result.status == "RUNNING"


# ---------------------------------------------------------------------------
# 4. No snapshot -> defined empty result
# ---------------------------------------------------------------------------

def test_no_snapshot_returns_none(db):
    result = get_latest_profiling_snapshot(999)
    assert result is None


# ---------------------------------------------------------------------------
# 5. Scoped correctly by source_id
# ---------------------------------------------------------------------------

def test_scoped_by_source_id(db):
    _insert_snapshot(db, source_id=1, snapshot_version=5, status="COMPLETE")
    other_source_id = _insert_snapshot(db, source_id=2, snapshot_version=99, status="COMPLETE")

    result = get_latest_profiling_snapshot(2)

    assert result is not None
    assert result.id == other_source_id
    assert result.source_id == 2

    result_1 = get_latest_profiling_snapshot(1)
    assert result_1 is not None
    assert result_1.source_id == 1
    assert result_1.snapshot_version == 5


def test_status_aware_selection_does_not_leak_across_sources(db):
    """Source 1 has no COMPLETE snapshot (must fall back to its own newest
    CANCELLED); source 2 has a COMPLETE snapshot older than its own newest
    CANCELLED (must prefer it). Neither source's fallback/preference may be
    influenced by the other's rows."""
    only_cancelled_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="CANCELLED")
    other_complete_id = _insert_snapshot(db, source_id=2, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=2, snapshot_version=2, status="CANCELLED")

    result_1 = get_latest_profiling_snapshot(1)
    assert result_1 is not None
    assert result_1.id == only_cancelled_id
    assert result_1.status == "CANCELLED"

    result_2 = get_latest_profiling_snapshot(2)
    assert result_2 is not None
    assert result_2.id == other_complete_id
    assert result_2.status == "COMPLETE"


# ---------------------------------------------------------------------------
# 6. No writes
# ---------------------------------------------------------------------------

def test_resolver_performs_no_writes(db):
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")

    before = [dict(r) for r in db.execute("SELECT * FROM profiling_snapshots").fetchall()]
    get_latest_profiling_snapshot(1)
    get_latest_profiling_snapshot(999)  # no-result path too
    after = [dict(r) for r in db.execute("SELECT * FROM profiling_snapshots").fetchall()]

    assert before == after


# ---------------------------------------------------------------------------
# Return-type sanity
# ---------------------------------------------------------------------------

def test_returns_profiling_snapshot_ref_dataclass(db):
    _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE", schema_snapshot_id=42)

    result = get_latest_profiling_snapshot(1)

    assert isinstance(result, ProfilingSnapshotRef)
    assert result.schema_snapshot_id == 42


# ---------------------------------------------------------------------------
# Phase 2F.2 — connection-aware resolver (optional `conn` parameter)
#
# _ConnSpy wraps a real sqlite3.Connection and records close()/commit()/
# rollback() calls without forwarding them, so the underlying connection
# stays open/uncommitted regardless of what the resolver does — letting
# tests assert on ownership behavior directly instead of inferring it from
# side effects on a real connection.
# ---------------------------------------------------------------------------

class _ConnSpy:
    def __init__(self, conn: sqlite3.Connection, raise_on_execute: Exception | None = None) -> None:
        self._conn = conn
        self._raise_on_execute = raise_on_execute
        self.close_called = False
        self.commit_called = False
        self.rollback_called = False

    def execute(self, *args, **kwargs):
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return self._conn.execute(*args, **kwargs)

    def close(self) -> None:
        self.close_called = True

    def commit(self) -> None:
        self.commit_called = True

    def rollback(self) -> None:
        self.rollback_called = True

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@pytest.fixture()
def raw_conn():
    """Real in-memory sqlite connection, unpatched — used as a caller-supplied conn."""
    conn = _make_db()
    yield conn
    conn.close()


@pytest.fixture()
def guard_get_connection():
    """Patches get_connection to fail loudly if the resolver calls it while a
    conn was supplied — both a safety net (never touches the real DB_PATH)
    and the assertion body for the dedicated 'does not call' test."""
    with patch(
        "data.profiling_snapshot_resolver.get_connection",
        side_effect=AssertionError("get_connection must not be called when conn is supplied"),
    ) as mock:
        yield mock


# --- supplied-connection semantics (behaviorally identical to omitted-conn) ---

def test_supplied_connection_returns_latest_snapshot(raw_conn, guard_get_connection):
    _insert_snapshot(raw_conn, source_id=1, snapshot_version=1, status="COMPLETE")
    newest_id = _insert_snapshot(raw_conn, source_id=1, snapshot_version=2, status="COMPLETE")

    result = get_latest_profiling_snapshot(1, conn=raw_conn)

    assert result is not None
    assert result.id == newest_id
    assert result.snapshot_version == 2


def test_supplied_connection_preserves_complete_precedence_over_cancelled(raw_conn, guard_get_connection):
    complete_id = _insert_snapshot(raw_conn, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(raw_conn, source_id=1, snapshot_version=2, status="CANCELLED")

    result = get_latest_profiling_snapshot(1, conn=raw_conn)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


def test_supplied_connection_falls_back_when_no_complete(raw_conn, guard_get_connection):
    _insert_snapshot(raw_conn, source_id=1, snapshot_version=1, status="CANCELLED")
    running_id = _insert_snapshot(raw_conn, source_id=1, snapshot_version=2, status="RUNNING")

    result = get_latest_profiling_snapshot(1, conn=raw_conn)

    assert result is not None
    assert result.id == running_id
    assert result.status == "RUNNING"


def test_supplied_connection_preserves_source_scoping(raw_conn, guard_get_connection):
    _insert_snapshot(raw_conn, source_id=1, snapshot_version=5, status="COMPLETE")
    other_source_id = _insert_snapshot(raw_conn, source_id=2, snapshot_version=99, status="COMPLETE")

    result = get_latest_profiling_snapshot(2, conn=raw_conn)

    assert result is not None
    assert result.id == other_source_id
    assert result.source_id == 2


def test_supplied_connection_returns_none_when_missing(raw_conn, guard_get_connection):
    result = get_latest_profiling_snapshot(999, conn=raw_conn)
    assert result is None


# --- supplied-connection ownership contract ---

def test_supplied_connection_does_not_call_get_connection(raw_conn, guard_get_connection):
    get_latest_profiling_snapshot(1, conn=raw_conn)
    assert guard_get_connection.call_count == 0


def test_supplied_connection_is_not_closed(raw_conn, guard_get_connection):
    spy = _ConnSpy(raw_conn)
    get_latest_profiling_snapshot(1, conn=spy)
    assert spy.close_called is False


def test_supplied_connection_is_not_committed(raw_conn, guard_get_connection):
    spy = _ConnSpy(raw_conn)
    get_latest_profiling_snapshot(1, conn=spy)
    assert spy.commit_called is False


def test_supplied_connection_is_not_rolled_back(raw_conn, guard_get_connection):
    spy = _ConnSpy(raw_conn)
    get_latest_profiling_snapshot(1, conn=spy)
    assert spy.rollback_called is False


def test_supplied_connection_remains_usable_after_success(raw_conn, guard_get_connection):
    _insert_snapshot(raw_conn, source_id=1, snapshot_version=1, status="COMPLETE")

    get_latest_profiling_snapshot(1, conn=raw_conn)

    # Would raise sqlite3.ProgrammingError on a closed connection.
    row = raw_conn.execute("SELECT COUNT(*) AS n FROM profiling_snapshots").fetchone()
    assert row["n"] == 1


def test_supplied_connection_remains_usable_after_no_snapshot(raw_conn, guard_get_connection):
    get_latest_profiling_snapshot(999, conn=raw_conn)

    row = raw_conn.execute("SELECT COUNT(*) AS n FROM profiling_snapshots").fetchone()
    assert row["n"] == 0


def test_supplied_connection_remains_open_when_query_raises(raw_conn, guard_get_connection):
    spy = _ConnSpy(raw_conn, raise_on_execute=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        get_latest_profiling_snapshot(1, conn=spy)

    assert spy.close_called is False
    assert spy.commit_called is False
    assert spy.rollback_called is False


# --- internally owned connection: unchanged lifecycle, verified explicitly ---

def test_omitted_connection_calls_get_connection_once():
    conn = _make_db()
    _insert_snapshot(conn, source_id=1, snapshot_version=1, status="COMPLETE")
    spy = _ConnSpy(conn)
    try:
        with patch("data.profiling_snapshot_resolver.get_connection", return_value=spy) as mock_get_conn:
            get_latest_profiling_snapshot(1)
        assert mock_get_conn.call_count == 1
    finally:
        conn.close()


def test_omitted_connection_closes_owned_connection_after_success():
    conn = _make_db()
    _insert_snapshot(conn, source_id=1, snapshot_version=1, status="COMPLETE")
    spy = _ConnSpy(conn)
    try:
        with patch("data.profiling_snapshot_resolver.get_connection", return_value=spy):
            result = get_latest_profiling_snapshot(1)
        assert result is not None
        assert spy.close_called is True
    finally:
        conn.close()


def test_omitted_connection_closes_owned_connection_after_no_snapshot():
    conn = _make_db()
    spy = _ConnSpy(conn)
    try:
        with patch("data.profiling_snapshot_resolver.get_connection", return_value=spy):
            result = get_latest_profiling_snapshot(999)
        assert result is None
        assert spy.close_called is True
    finally:
        conn.close()


def test_omitted_connection_closes_owned_connection_when_query_raises():
    conn = _make_db()
    spy = _ConnSpy(conn, raise_on_execute=RuntimeError("boom"))
    try:
        with patch("data.profiling_snapshot_resolver.get_connection", return_value=spy):
            with pytest.raises(RuntimeError, match="boom"):
                get_latest_profiling_snapshot(1)
        assert spy.close_called is True
    finally:
        conn.close()


def test_omitted_connection_preserves_status_aware_selection_semantics(db):
    complete_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot(db, source_id=1, snapshot_version=2, status="CANCELLED")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


# --- backwards compatibility ---

def test_all_existing_source_id_only_call_style_remains_valid(db):
    """The pre-Phase-2F.2 call style — a single positional source_id, no conn —
    must keep working unchanged for all existing production call sites."""
    newest_id = _insert_snapshot(db, source_id=1, snapshot_version=1, status="COMPLETE")

    result = get_latest_profiling_snapshot(1)

    assert isinstance(result, ProfilingSnapshotRef)
    assert result.id == newest_id


# ---------------------------------------------------------------------------
# ProfilingSnapshotDetail / get_latest_profiling_snapshot_detail
#
# Phase 1F.0 — infrastructure-only. Selection policy must match the identity
# resolver above exactly (highest snapshot_version, status-independent); the
# only difference is the wider field set returned.
# ---------------------------------------------------------------------------

def _insert_snapshot_detail(
    db: sqlite3.Connection,
    source_id: int,
    snapshot_version: int,
    status: str,
    schema_snapshot_id: int = 1,
    mode: str = "full",
    created_at: str = "2026-01-01T00:00:00",
    completed_at: str | None = None,
    tables_total: int = 0,
    tables_profiled: int = 0,
    columns_profiled: int = 0,
    pii_columns_found: int = 0,
    total_rows_profiled: int = 0,
) -> int:
    cur = db.execute(
        "INSERT INTO profiling_snapshots "
        "(source_id, schema_snapshot_id, snapshot_version, status, mode, "
        "created_at, completed_at, tables_total, tables_profiled, "
        "columns_profiled, pii_columns_found, total_rows_profiled) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source_id,
            schema_snapshot_id,
            snapshot_version,
            status,
            mode,
            created_at,
            completed_at,
            tables_total,
            tables_profiled,
            columns_profiled,
            pii_columns_found,
            total_rows_profiled,
        ),
    )
    db.commit()
    return cur.lastrowid


# 1. Correct mapping of every field.
def test_detail_maps_every_field(db):
    snap_id = _insert_snapshot_detail(
        db,
        source_id=1,
        snapshot_version=3,
        status="COMPLETE",
        schema_snapshot_id=7,
        mode="incremental",
        created_at="2026-02-01T10:00:00",
        completed_at="2026-02-01T10:05:00",
        tables_total=10,
        tables_profiled=9,
        columns_profiled=80,
        pii_columns_found=4,
        total_rows_profiled=123456,
    )

    result = get_latest_profiling_snapshot_detail(1)

    assert isinstance(result, ProfilingSnapshotDetail)
    assert result.id == snap_id
    assert result.source_id == 1
    assert result.snapshot_version == 3
    assert result.status == "COMPLETE"
    assert result.schema_snapshot_id == 7
    assert result.mode == "incremental"
    assert result.created_at == "2026-02-01T10:00:00"
    assert result.completed_at == "2026-02-01T10:05:00"
    assert result.tables_total == 10
    assert result.tables_profiled == 9
    assert result.columns_profiled == 80
    assert result.pii_columns_found == 4
    assert result.total_rows_profiled == 123456


# 2. Nullable completed_at preserved.
def test_detail_nullable_completed_at_preserved(db):
    _insert_snapshot_detail(
        db, source_id=1, snapshot_version=1, status="RUNNING", completed_at=None,
    )

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.completed_at is None


# 3. Integer counters preserved.
def test_detail_integer_counters_preserved(db):
    _insert_snapshot_detail(
        db,
        source_id=1,
        snapshot_version=1,
        status="COMPLETE",
        tables_total=17,
        tables_profiled=13,
        columns_profiled=55,
        pii_columns_found=2,
        total_rows_profiled=987654,
    )

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.tables_total == 17
    assert result.tables_profiled == 13
    assert result.columns_profiled == 55
    assert result.pii_columns_found == 2
    assert result.total_rows_profiled == 987654


# 4. created_at preserved exactly.
def test_detail_created_at_preserved_exactly(db):
    _insert_snapshot_detail(
        db, source_id=1, snapshot_version=1, status="COMPLETE",
        created_at="2025-12-31T23:59:59.123456",
    )

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.created_at == "2025-12-31T23:59:59.123456"


# 5. Latest snapshot_version still selected.
def test_detail_selects_highest_snapshot_version(db):
    _insert_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")
    newest_id = _insert_snapshot_detail(db, source_id=1, snapshot_version=2, status="COMPLETE")
    _insert_snapshot_detail(db, source_id=2, snapshot_version=1, status="COMPLETE")  # different source, irrelevant

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.id == newest_id
    assert result.snapshot_version == 2


# 6. Older COMPLETE beats newer CANCELLED.
def test_detail_older_complete_beats_newer_cancelled(db):
    complete_id = _insert_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot_detail(db, source_id=1, snapshot_version=2, status="CANCELLED")

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.id == complete_id
    assert result.status == "COMPLETE"


# 7. No COMPLETE at all -> falls back to newest any-status (RUNNING here).
def test_detail_falls_back_to_newest_when_no_complete(db):
    _insert_snapshot_detail(db, source_id=1, snapshot_version=1, status="CANCELLED")
    running_id = _insert_snapshot_detail(db, source_id=1, snapshot_version=2, status="RUNNING")

    result = get_latest_profiling_snapshot_detail(1)

    assert result is not None
    assert result.id == running_id
    assert result.status == "RUNNING"


# 7b. Both functions agree on which snapshot is "latest" — they share the
# same selection query, so this is a guard against them ever diverging.
def test_detail_and_ref_agree_on_selection(db):
    complete_id = _insert_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")
    _insert_snapshot_detail(db, source_id=1, snapshot_version=2, status="CANCELLED")

    ref = get_latest_profiling_snapshot(1)
    detail = get_latest_profiling_snapshot_detail(1)

    assert ref.id == detail.id == complete_id


# 8. No snapshot returns None.
def test_detail_no_snapshot_returns_none(db):
    result = get_latest_profiling_snapshot_detail(999)
    assert result is None


# 9. Source scoping preserved.
def test_detail_scoped_by_source_id(db):
    _insert_snapshot_detail(db, source_id=1, snapshot_version=5, status="COMPLETE")
    other_source_id = _insert_snapshot_detail(db, source_id=2, snapshot_version=99, status="COMPLETE")

    result = get_latest_profiling_snapshot_detail(2)

    assert result is not None
    assert result.id == other_source_id
    assert result.source_id == 2

    result_1 = get_latest_profiling_snapshot_detail(1)
    assert result_1 is not None
    assert result_1.source_id == 1
    assert result_1.snapshot_version == 5


# 10. No writes occur.
def test_detail_resolver_performs_no_writes(db):
    _insert_snapshot_detail(db, source_id=1, snapshot_version=1, status="COMPLETE")

    before = [dict(r) for r in db.execute("SELECT * FROM profiling_snapshots").fetchall()]
    get_latest_profiling_snapshot_detail(1)
    get_latest_profiling_snapshot_detail(999)  # no-result path too
    after = [dict(r) for r in db.execute("SELECT * FROM profiling_snapshots").fetchall()]

    assert before == after


# 11. Existing identity resolver remains unchanged.
def test_identity_resolver_unaffected_by_detail_addition(db):
    """Guards against the schema/detail addition leaking into the identity
    resolver's shape or selection behavior."""
    _insert_snapshot_detail(
        db,
        source_id=1,
        snapshot_version=1,
        status="COMPLETE",
        schema_snapshot_id=9,
        mode="incremental",
        tables_total=42,
    )

    result = get_latest_profiling_snapshot(1)

    assert isinstance(result, ProfilingSnapshotRef)
    assert not isinstance(result, ProfilingSnapshotDetail)
    assert result.id is not None
    assert result.source_id == 1
    assert result.snapshot_version == 1
    assert result.status == "COMPLETE"
    assert result.schema_snapshot_id == 9
    assert not hasattr(result, "mode")
    assert not hasattr(result, "tables_total")
