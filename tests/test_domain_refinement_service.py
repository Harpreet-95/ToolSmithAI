"""
Phase 2G.11 — pre-migration behavioral baseline for
data.domain_refinement_service.analyze_rule_refinement()'s inline
"latest profiling snapshot" lookup.

Read-only investigation phase: no production code is touched here. These
tests pin down analyze_rule_refinement()'s CURRENT (pre-migration) behavior
so a later resolver migration (mirroring Phases 2G.5-2G.9's Dictionary/
Domain/Entity Service work) can be proven to preserve it exactly.

Key findings this file's tests encode:

- The inline lookup ("SELECT id FROM profiling_snapshots WHERE source_id = ?
  ORDER BY snapshot_version DESC LIMIT 1") already executes on the SAME
  connection opened for the rest of the initial read block (ownership check,
  domain_learning_rules read, domain_assignments read, profiling_table_profiles
  read) - there is exactly one get_connection() call for that whole block
  today. A future resolver migration must preserve this by passing conn=conn,
  exactly like domain_service.py/entity_service.py/dictionary_service.py did.

- Unlike generate_domain_assignments()/generate_entity_assignments(), a
  missing profiling_snapshots row is NOT an error here: snap_row is simply
  None, profile_map stays empty, and the function proceeds normally. This
  must remain true after migration.

- The function's own docstring claims "Read-only - makes no writes to the
  database", but this is NOT accurate for the full function: when at least
  one rule is flagged, a second, separate connection persists suggestion
  rows into domain_rule_refinement_suggestions via an idempotent
  ON CONFLICT DO NOTHING upsert, then commits. Only the FIRST (read) block is
  actually read-only. This file's read-only test is scoped precisely to that
  first block, not the whole function - a broader "makes no writes" claim
  (as domain_quality_service.py's test suite makes for analyze_domain_quality,
  which really is fully read-only) would be false here.

- The result shape differs by path: the "not flagged" short-circuit returns
  {source_id, flagged_rules, candidate_refinements, projected_accuracy_improvement}
  (flagged_rules always 0, candidate_refinements always []); the "flagged"
  path returns those same four keys PLUS total_flagged_tables,
  total_refineable_tables, suggestions_new, suggestions_skipped.

Run from the project root:
    venv/Scripts/pytest tests/test_domain_refinement_service.py -v
"""
import ast
import inspect
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from data.domain_refinement_service import analyze_rule_refinement
from data.profiling_snapshot_resolver import (
    ProfilingSnapshotRef,
    get_latest_profiling_snapshot,
)

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test Source',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE domain_learning_rules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       INTEGER NOT NULL,
        pattern_type    TEXT NOT NULL,
        pattern_value   TEXT NOT NULL,
        domain          TEXT NOT NULL,
        confidence      REAL NOT NULL DEFAULT 0.8,
        approval_status TEXT NOT NULL DEFAULT 'PENDING',
        created_by      TEXT NOT NULL DEFAULT 'system',
        approved_by     TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        approved_at     TEXT,
        active          INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE domain_assignments (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id              INTEGER NOT NULL,
        profiling_snapshot_id  INTEGER NOT NULL DEFAULT 1,
        table_fqn              TEXT    NOT NULL,
        domain                 TEXT    NOT NULL,
        confidence             REAL    NOT NULL DEFAULT 0.0,
        evidence_json          TEXT    NOT NULL DEFAULT '[]',
        competing_domains_json TEXT    NOT NULL DEFAULT '[]',
        created_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_snapshots (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id          INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1,
        snapshot_version   INTEGER NOT NULL DEFAULT 1,
        status             TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_table_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        table_name            TEXT    NOT NULL,
        schema_name           TEXT    NOT NULL DEFAULT 'dbo',
        table_class           TEXT,
        pii_column_count      INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count   INTEGER NOT NULL DEFAULT 0,
        fk_count              INTEGER NOT NULL DEFAULT 0,
        referenced_by_count   INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE domain_rule_refinement_suggestions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        parent_rule_id   INTEGER NOT NULL,
        pattern_type     TEXT    NOT NULL,
        pattern_value    TEXT    NOT NULL,
        suggested_domain TEXT    NOT NULL,
        support_count    INTEGER NOT NULL DEFAULT 0,
        confidence       REAL    NOT NULL DEFAULT 0.0,
        approval_status  TEXT    NOT NULL DEFAULT 'PENDING',
        created_at       TEXT    NOT NULL,
        approved_at      TEXT,
        approved_by      TEXT,
        active           INTEGER NOT NULL DEFAULT 0
    );

    CREATE UNIQUE INDEX idx_drrs_parent_type_val
        ON domain_rule_refinement_suggestions (parent_rule_id, pattern_type, pattern_value);
"""


class _NoClose:
    """Wraps sqlite3.Connection and suppresses close() so the in-memory DB
    survives across multiple get_connection() calls within a single test -
    mirrors the existing convention in tests/test_domain_quality_service.py."""

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
    """Fresh in-memory DB. Only data.domain_refinement_service.get_connection
    needs patching - analyze_rule_refinement() (Phase 2G.12) always calls the
    resolver with conn=conn, so the resolver never opens its own connection
    and data.profiling_snapshot_resolver.get_connection is never reached."""
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.domain_refinement_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/test_domain_quality_service.py's conventions)
# ---------------------------------------------------------------------------

def _seed_source(db: sqlite3.Connection, user_id: str = "user-1") -> int:
    cur = db.execute("INSERT INTO data_source_connections (user_id) VALUES (?)", (user_id,))
    db.commit()
    return cur.lastrowid


def _seed_rule(
    db: sqlite3.Connection, source_id: int, pattern_type: str, pattern_value: str, domain: str,
) -> int:
    cur = db.execute(
        "INSERT INTO domain_learning_rules "
        "(source_id, pattern_type, pattern_value, domain, approval_status, active) "
        "VALUES (?, ?, ?, ?, 'APPROVED', 1)",
        (source_id, pattern_type, pattern_value, domain),
    )
    db.commit()
    return cur.lastrowid


def _seed_assignment(
    db: sqlite3.Connection, source_id: int, table_fqn: str, domain: str,
    confidence: float, rule_pattern_type: str, rule_pattern_value: str,
) -> None:
    evidence = f"[\"learned rule [{rule_pattern_type}] '{rule_pattern_value}' \\u2192 {domain}\"]"
    db.execute(
        "INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence, evidence_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (source_id, table_fqn, domain, confidence, evidence),
    )
    db.commit()


def _seed_unrelated_padding(db: sqlite3.Connection, source_id: int, count: int) -> None:
    """Adds `count` non-Unknown, non-matching domain_assignments rows so
    total_assigned grows without affecting tables_matched for any learned
    rule - used to keep share_of_assigned at or below the 0.25 flag
    threshold in scenarios that are meant to isolate a different signal."""
    for i in range(count):
        db.execute(
            "INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence, evidence_json) "
            "VALUES (?, ?, 'Sales', 0.7, '[]')",
            (source_id, f"dbo.padding_{i}"),
        )
    db.commit()


def _seed_profiling_snapshot(
    db: sqlite3.Connection, source_id: int, snapshot_version: int, status: str = "COMPLETE",
) -> int:
    cur = db.execute(
        "INSERT INTO profiling_snapshots (source_id, snapshot_version, status) VALUES (?, ?, ?)",
        (source_id, snapshot_version, status),
    )
    db.commit()
    return cur.lastrowid


def _seed_table_profile(
    db: sqlite3.Connection, snap_id: int, source_id: int, table_fqn: str,
    table_name: str, table_class: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, table_class) "
        "VALUES (?, ?, ?, ?, ?)",
        (snap_id, source_id, table_fqn, table_name, table_class),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Domain-spread precedence scenario helper.
#
# Seeds one rule ("PREFIX", "edu", "Operations") matched by 4 assignments,
# plus 12 unrelated assignments so total_assigned = 16 and
# share_of_assigned = 4/16 = 0.25 (NOT > the 0.25 threshold - deliberately
# kept from independently triggering the share-of-assigned flag). Confidence
# is kept at 0.8 (above the 0.60 low-confidence flag threshold) and
# tables_matched = 4 (well under the 100 high-coverage threshold). This
# isolates "is the rule flagged" to depend on exactly one thing: whether the
# 4 tables' profiling_table_profiles rows (and therefore their
# detect_table_domain() outputs) are visible via profile_map - i.e. whether
# the CORRECT profiling_snapshots row was selected as "latest".
# ---------------------------------------------------------------------------

def _seed_domain_spread_rule(db: sqlite3.Connection, source_id: int) -> None:
    _seed_rule(db, source_id, "PREFIX", "edu", "Operations")
    _seed_assignment(db, source_id, "dbo.edu_ref", "Operations", 0.8, "PREFIX", "edu")
    _seed_assignment(db, source_id, "dbo.edu_rep", "Operations", 0.8, "PREFIX", "edu")
    _seed_assignment(db, source_id, "dbo.edu_payroll_history", "Operations", 0.8, "PREFIX", "edu")
    _seed_assignment(db, source_id, "dbo.edu_student_enrollment", "Operations", 0.8, "PREFIX", "edu")
    for i in range(12):
        db.execute(
            "INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence, evidence_json) "
            "VALUES (?, ?, 'Sales', 0.7, '[]')",
            (source_id, f"dbo.unrelated_{i}"),
        )
    db.commit()


def _seed_varied_domain_profiles(db: sqlite3.Connection, snap_id: int, source_id: int) -> None:
    """4 distinct table_class/name combinations -> 4 distinct generic domains
    (reuses the exact tuples proven in tests/test_domain_quality_service.py's
    test_flags_and_recommendation_for_domain_spread_rule to yield
    unique_generic_domains == 4)."""
    _seed_table_profile(db, snap_id, source_id, "dbo.edu_ref", "edu_ref", table_class="REFERENCE")
    _seed_table_profile(db, snap_id, source_id, "dbo.edu_rep", "edu_rep", table_class="REPORTING")
    _seed_table_profile(db, snap_id, source_id, "dbo.edu_payroll_history", "edu_payroll_history")
    _seed_table_profile(db, snap_id, source_id, "dbo.edu_student_enrollment", "edu_student_enrollment")


def _seed_uniform_domain_profiles(db: sqlite3.Connection, snap_id: int, source_id: int) -> None:
    """Same 4 tables, but all REFERENCE -> only 1 unique generic domain, well
    under the domain-spread flag threshold (3). Used as the 'wrong, older'
    snapshot's data: if this data were incorrectly selected instead of the
    newer snapshot's varied data, the rule would NOT be flagged."""
    for fqn, name in [
        ("dbo.edu_ref", "edu_ref"),
        ("dbo.edu_rep", "edu_rep"),
        ("dbo.edu_payroll_history", "edu_payroll_history"),
        ("dbo.edu_student_enrollment", "edu_student_enrollment"),
    ]:
        _seed_table_profile(db, snap_id, source_id, fqn, name, table_class="REFERENCE")


# ---------------------------------------------------------------------------
# 1 - highest snapshot_version wins
# ---------------------------------------------------------------------------

def test_selects_highest_snapshot_version(db):
    src = _seed_source(db)
    _seed_domain_spread_rule(db, src)

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_uniform_domain_profiles(db, snap1, src)  # would NOT flag if wrongly used

    snap2 = _seed_profiling_snapshot(db, src, snapshot_version=2, status="COMPLETE")
    _seed_varied_domain_profiles(db, snap2, src)  # DOES flag - proves snap2 (highest) wins

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1


# ---------------------------------------------------------------------------
# 2 - older COMPLETE beats newer CANCELLED
# ---------------------------------------------------------------------------

def test_older_complete_snapshot_wins_over_newer_cancelled(db):
    src = _seed_source(db)
    _seed_domain_spread_rule(db, src)

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_varied_domain_profiles(db, snap1, src)  # DOES flag - proves snap1 (older COMPLETE) wins

    # Newer CANCELLED snapshot - no profiling_table_profiles rows under it at
    # all (mirrors a real partial/cancelled run that never reached these
    # tables). The resolver must never let this newer, empty snapshot shadow
    # snap1's older, usable COMPLETE data (see
    # data/profiling_snapshot_resolver.py policy), so the rule IS flagged.
    _seed_profiling_snapshot(db, src, snapshot_version=2, status="CANCELLED")

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1


# ---------------------------------------------------------------------------
# 3 - older COMPLETE beats newer RUNNING
# ---------------------------------------------------------------------------

def test_older_complete_snapshot_wins_over_newer_running(db):
    src = _seed_source(db)
    _seed_domain_spread_rule(db, src)

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_varied_domain_profiles(db, snap1, src)  # DOES flag - proves snap1 (older COMPLETE) wins

    # Newer RUNNING snapshot - no profiling_table_profiles rows under it yet.
    # Must not shadow snap1's older, usable COMPLETE data.
    _seed_profiling_snapshot(db, src, snapshot_version=2, status="RUNNING")

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1


# ---------------------------------------------------------------------------
# 4 - source scoping
# ---------------------------------------------------------------------------

def test_source_scoping_two_sources(db):
    src_a = _seed_source(db, "user-A")
    src_b = _seed_source(db, "user-B")

    _seed_domain_spread_rule(db, src_a)
    snap_a = _seed_profiling_snapshot(db, src_a, snapshot_version=1, status="COMPLETE")
    _seed_uniform_domain_profiles(db, snap_a, src_a)  # source A: not flagged

    # source B has a FAR higher snapshot_version and the rich, would-flag
    # data, for identically-named tables - must never leak into source A.
    _seed_domain_spread_rule(db, src_b)
    snap_b = _seed_profiling_snapshot(db, src_b, snapshot_version=99, status="COMPLETE")
    _seed_varied_domain_profiles(db, snap_b, src_b)

    result_a = analyze_rule_refinement(src_a, "user-A")
    assert result_a is not None
    assert result_a["flagged_rules"] == 0

    # user-A cannot read source B
    assert analyze_rule_refinement(src_b, "user-A") is None


# ---------------------------------------------------------------------------
# 5 - no-snapshot behavior: graceful, NOT an exception
# ---------------------------------------------------------------------------

def test_no_profiling_snapshot_is_graceful_not_an_exception(db):
    """Unlike generate_domain_assignments()/generate_entity_assignments(),
    this function raises nothing when no profiling_snapshots row exists for
    the source: snap_row is None, profile_map stays {}, and the function
    still returns its normal (short-circuit, not-flagged) shape."""
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    _seed_unrelated_padding(db, src, count=3)  # keeps share_of_assigned <= 0.25
    # No profiling_snapshots row seeded at all.

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 0
    assert result["candidate_refinements"] == []
    assert result["projected_accuracy_improvement"] == 0.0


def test_unowned_source_returns_none(db):
    src = _seed_source(db, "user-1")
    assert analyze_rule_refinement(src, "user-2") is None
    assert analyze_rule_refinement(999999, "user-1") is None


# ---------------------------------------------------------------------------
# 6 - result shape: differs between the not-flagged short-circuit and the
# full flagged path.
# ---------------------------------------------------------------------------

def test_result_shape_not_flagged_path(db):
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    _seed_unrelated_padding(db, src, count=3)  # keeps share_of_assigned <= 0.25
    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 0
    assert set(result.keys()) == {
        "source_id", "flagged_rules", "candidate_refinements", "projected_accuracy_improvement",
    }
    assert result["source_id"] == src
    assert result["candidate_refinements"] == []
    assert isinstance(result["projected_accuracy_improvement"], float)


def _seed_real_candidate_scenario(db: sqlite3.Connection) -> int:
    """A minimal scenario that reaches actual sub-rule candidate generation
    (not just the flagged/not-flagged boundary): 3 tables sharing the
    'edu_orders_*' prefix (discriminator token 'orders'), all classified
    REPORTING (-> 'Reporting & Analytics', which differs from the parent
    rule's 'Operations'). With only these 3 assignments total,
    share_of_assigned = 3/3 = 1.0 > 0.25, so the rule is flagged purely via
    the share-of-assigned criterion (independent of domain spread), and the
    'orders' sub-group has support_count = 3 == _MIN_REFINEMENT_SUPPORT,
    confidence 1.0 >= _MIN_REFINEMENT_CONFIDENCE, producing one real
    candidate and one real persisted suggestion row."""
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    for i in range(1, 4):
        fqn = f"dbo.edu_orders_{i}"
        name = f"edu_orders_{i}"
        _seed_assignment(db, src, fqn, "Operations", 0.8, "PREFIX", "edu")
    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    for i in range(1, 4):
        _seed_table_profile(
            db, snap, src, f"dbo.edu_orders_{i}", f"edu_orders_{i}", table_class="REPORTING",
        )
    return src


def test_result_shape_flagged_path_with_real_candidate(db):
    src = _seed_real_candidate_scenario(db)

    result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1
    assert set(result.keys()) == {
        "source_id", "flagged_rules", "total_flagged_tables", "total_refineable_tables",
        "candidate_refinements", "projected_accuracy_improvement",
        "suggestions_new", "suggestions_skipped",
    }
    assert result["total_flagged_tables"] == 3
    assert result["total_refineable_tables"] == 3
    assert len(result["candidate_refinements"]) == 1
    cand = result["candidate_refinements"][0]
    assert cand["pattern_type"] == "TOKEN"
    assert cand["pattern_value"] == "orders"
    assert cand["suggested_domain"] == "Reporting & Analytics"
    assert cand["support_count"] == 3
    assert result["suggestions_new"] == 1
    assert result["suggestions_skipped"] == 0


# ---------------------------------------------------------------------------
# 7 - the function is NOT fully read-only despite its docstring: a flagged
# rule causes a real, committed write to domain_rule_refinement_suggestions.
# This is the current, pre-migration reality this baseline documents (not a
# behavior this phase is changing).
# ---------------------------------------------------------------------------

def test_flagged_rule_persists_real_suggestion_row(db):
    src = _seed_real_candidate_scenario(db)

    analyze_rule_refinement(src, "user-1")

    rows = db.execute(
        "SELECT * FROM domain_rule_refinement_suggestions WHERE source_id = ?", (src,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pattern_type"] == "TOKEN"
    assert rows[0]["pattern_value"] == "orders"
    assert rows[0]["suggested_domain"] == "Reporting & Analytics"
    assert rows[0]["approval_status"] == "PENDING"
    assert rows[0]["active"] == 0


# ---------------------------------------------------------------------------
# 8 - connection lifecycle: today, the snapshot lookup shares the SAME
# get_connection() call as the rest of the initial read block. A future
# resolver migration must preserve this (via conn=conn) rather than
# introducing a second, resolver-owned connection.
# ---------------------------------------------------------------------------

def _make_file_db(tmp_path, name: str = "refine.db") -> str:
    db_path = str(tmp_path / name)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _file_conn(db_path: str) -> sqlite3.Connection:
    c = sqlite3.connect(db_path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def test_connection_count_not_flagged_path_is_one(tmp_path):
    """When the short-circuit (not-flagged) path is taken, the write block
    (a second get_connection() call) is never reached - exactly one
    connection is opened for the whole call."""
    db_path = _make_file_db(tmp_path)

    seed_conn = _file_conn(db_path)
    src = _seed_source(seed_conn, "user-1")
    _seed_rule(seed_conn, src, "PREFIX", "edu", "Operations")
    _seed_assignment(seed_conn, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    _seed_unrelated_padding(seed_conn, src, count=3)  # keeps share_of_assigned <= 0.25
    snap = _seed_profiling_snapshot(seed_conn, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(seed_conn, snap, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")
    seed_conn.close()

    with patch(
        "data.domain_refinement_service.get_connection",
        side_effect=lambda: _file_conn(db_path),
    ) as mock_conn:
        result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 0
    assert mock_conn.call_count == 1


def test_connection_count_flagged_path_is_two(tmp_path):
    """When at least one rule is flagged, a second get_connection() call
    opens the write/persistence connection - two total."""
    db_path = _make_file_db(tmp_path)

    seed_conn = _file_conn(db_path)
    src = _seed_source(seed_conn, "user-1")
    _seed_rule(seed_conn, src, "PREFIX", "edu", "Operations")
    for i in range(1, 4):
        _seed_assignment(seed_conn, src, f"dbo.edu_orders_{i}", "Operations", 0.8, "PREFIX", "edu")
    snap = _seed_profiling_snapshot(seed_conn, src, snapshot_version=1, status="COMPLETE")
    for i in range(1, 4):
        _seed_table_profile(
            seed_conn, snap, src, f"dbo.edu_orders_{i}", f"edu_orders_{i}", table_class="REPORTING",
        )
    seed_conn.close()

    with patch(
        "data.domain_refinement_service.get_connection",
        side_effect=lambda: _file_conn(db_path),
    ) as mock_conn:
        result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1
    assert mock_conn.call_count == 2


# ---------------------------------------------------------------------------
# 9 - the initial read block itself is read-only (writes only ever happen on
# the second, write-phase connection - see test 7 above).
# ---------------------------------------------------------------------------

class _ChangesTrackingConn:
    """Wraps sqlite3.Connection and records total_changes at close() time -
    total_changes is unreadable on an already-closed sqlite3 connection, so
    this must be captured at the moment of closing, not afterward (mirrors
    _MarkingConnWrapper in tests/test_domain_entity_assignment_lock.py)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.total_changes_at_close: int | None = None

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        self.total_changes_at_close = self._conn.total_changes
        self._conn.close()


def test_initial_read_block_is_read_only(tmp_path):
    db_path = _make_file_db(tmp_path)

    seed_conn = _file_conn(db_path)
    src = _seed_source(seed_conn, "user-1")
    _seed_rule(seed_conn, src, "PREFIX", "edu", "Operations")
    for i in range(1, 4):
        _seed_assignment(seed_conn, src, f"dbo.edu_orders_{i}", "Operations", 0.8, "PREFIX", "edu")
    snap = _seed_profiling_snapshot(seed_conn, src, snapshot_version=1, status="COMPLETE")
    for i in range(1, 4):
        _seed_table_profile(
            seed_conn, snap, src, f"dbo.edu_orders_{i}", f"edu_orders_{i}", table_class="REPORTING",
        )
    seed_conn.close()

    wrapped_connections: list[_ChangesTrackingConn] = []

    def _tracking_conn() -> _ChangesTrackingConn:
        wrapper = _ChangesTrackingConn(_file_conn(db_path))
        wrapped_connections.append(wrapper)
        return wrapper

    with patch("data.domain_refinement_service.get_connection", side_effect=_tracking_conn):
        result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert result["flagged_rules"] == 1  # confirms the write-phase connection DOES run
    assert len(wrapped_connections) == 2
    read_block_conn, write_block_conn = wrapped_connections
    assert read_block_conn.total_changes_at_close == 0
    # Contrast: the write-phase connection is where the actual commit happens.
    assert write_block_conn.total_changes_at_close > 0


# ---------------------------------------------------------------------------
# Phase 2G.13 — migration-proof tests: analyze_rule_refinement() now
# delegates to the shared resolver instead of running its own inline SQL.
# Mirrors the Domain/Entity Service migration-proof tests in
# tests/test_domain_entity_assignment_lock.py and domain_quality_service's
# own test_delegates_to_shared_resolver.
# ---------------------------------------------------------------------------

def test_resolver_called_once_with_source_id_and_caller_conn(tmp_path):
    """Proves: the resolver is called exactly once, with source_id and the
    exact connection object analyze_rule_refinement() itself opened for the
    initial read block (not a new one), and the resolver never opens its own
    connection."""
    db_path = _make_file_db(tmp_path)

    seed_conn = _file_conn(db_path)
    src = _seed_source(seed_conn, "user-1")
    _seed_rule(seed_conn, src, "PREFIX", "edu", "Operations")
    _seed_assignment(seed_conn, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    _seed_unrelated_padding(seed_conn, src, count=3)
    snap = _seed_profiling_snapshot(seed_conn, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(seed_conn, snap, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")
    seed_conn.close()

    opened_connections: list = []

    def _tracking_conn():
        c = _file_conn(db_path)
        opened_connections.append(c)
        return c

    resolver_calls: list = []
    real_resolver = get_latest_profiling_snapshot

    def _tracking_resolver(source_id_arg, conn=None):
        # Captured at call time - the function goes on to open a second,
        # write-phase connection only if flagged, which never happens in
        # this not-flagged scenario, but we still compare against the most
        # recently opened connection at the moment of the resolver call.
        most_recently_opened = opened_connections[-1] if opened_connections else None
        resolver_calls.append((source_id_arg, conn, most_recently_opened))
        return real_resolver(source_id_arg, conn=conn)

    mock_resolver_get_connection = MagicMock()
    with patch("data.domain_refinement_service.get_connection", side_effect=_tracking_conn), \
         patch("data.domain_refinement_service.get_latest_profiling_snapshot", _tracking_resolver), \
         patch("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection):
        result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    assert len(resolver_calls) == 1
    called_source_id, called_conn, most_recently_opened_at_call_time = resolver_calls[0]
    assert called_source_id == src
    assert called_conn is most_recently_opened_at_call_time
    assert mock_resolver_get_connection.call_count == 0


def test_resolver_result_drives_downstream_profile_lookup(db):
    """Patches the resolver symbol imported into domain_refinement_service to
    return a controlled ProfilingSnapshotRef pointing at a real but
    deliberately LOWER-version profiling snapshot than the source's true
    highest-version one - so any leftover inline query (which always ordered
    by snapshot_version DESC) could never have picked it. The downstream
    profiling_table_profiles read is scoped only by profiling_snapshot_id, so
    seeing this lower-version snapshot's (flagging) data drive the result
    proves the function now uses snapshot.id from the resolver's return
    value directly, not any inline SELECT."""
    src = _seed_source(db)
    _seed_domain_spread_rule(db, src)

    # The real "latest" snapshot per DB ordering - high version, uniform
    # (not-flagged) data. If any inline query still existed, THIS is the row
    # it would select.
    real_latest_id = _seed_profiling_snapshot(db, src, snapshot_version=5, status="COMPLETE")
    _seed_uniform_domain_profiles(db, real_latest_id, src)

    # A real but LOWER-version snapshot carrying the varied (flagging) data.
    lower_id = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_varied_domain_profiles(db, lower_id, src)

    fake_ref = ProfilingSnapshotRef(
        id=lower_id, source_id=src, snapshot_version=1,
        status="COMPLETE", schema_snapshot_id=1,
    )

    with patch(
        "data.domain_refinement_service.get_latest_profiling_snapshot",
        lambda source_id, conn=None: fake_ref,
    ):
        result = analyze_rule_refinement(src, "user-1")

    assert result is not None
    # Only reachable if profile_map was built from lower_id's varied data
    # (snapshot.id from the resolver's fake return value), not real_latest_id.
    assert result["flagged_rules"] == 1


def test_static_exactly_one_resolver_import_and_call_with_conn():
    """Static/AST proof: exactly one resolver import at module level, and
    exactly one get_latest_profiling_snapshot(...) call inside
    analyze_rule_refinement() itself, with source_id passed positionally and
    conn=conn passed by keyword."""
    with open("data/domain_refinement_service.py", encoding="utf-8") as f:
        module_source = f.read()

    import_lines = [
        line for line in module_source.splitlines()
        if line.strip() == "from data.profiling_snapshot_resolver import get_latest_profiling_snapshot"
    ]
    assert len(import_lines) == 1, "expected exactly one resolver import"

    func_source = inspect.getsource(analyze_rule_refinement)
    tree = ast.parse(func_source)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_latest_profiling_snapshot"
    ]
    assert len(calls) == 1, "expected exactly one get_latest_profiling_snapshot(...) call"

    call = calls[0]
    positional_names = [a.id for a in call.args if isinstance(a, ast.Name)]
    assert "source_id" in positional_names

    keyword_values = {kw.arg: kw.value for kw in call.keywords}
    assert "conn" in keyword_values
    conn_value = keyword_values["conn"]
    assert isinstance(conn_value, ast.Name) and conn_value.id == "conn"


def test_no_inline_latest_profiling_snapshot_sql_remains():
    """Confirms the duplicated inline lookup this phase removed is fully
    gone from the module - not just from analyze_rule_refinement() itself."""
    with open("data/domain_refinement_service.py", encoding="utf-8") as f:
        source = f.read()

    assert "FROM profiling_snapshots" not in source
    assert "ORDER BY snapshot_version DESC LIMIT 1" not in source
