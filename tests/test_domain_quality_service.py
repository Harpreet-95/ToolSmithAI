"""
Tests for data.domain_quality_service.analyze_domain_quality — Phase 1D of
the Profiling Snapshot Resolver migration.

No pre-existing tests covered this function before this phase (it has zero
callers anywhere in the codebase — confirmed by repository search — so this
file is net-new, not an extension of prior coverage). These tests both pin
down the function's existing (pre-migration-equivalent) behavior for a
realistic seeded scenario, and prove the Phase 1D resolver migration
preserved that behavior exactly, including the current, intentionally
status-independent snapshot-selection policy (highest snapshot_version
wins, regardless of status).

Run from the project root:
    venv/Scripts/pytest tests/test_domain_quality_service.py -v
"""
import sqlite3
from unittest.mock import patch

import pytest

from data.domain_quality_service import analyze_domain_quality
from data.profiling_snapshot_resolver import (
    get_latest_profiling_snapshot as _real_get_latest_profiling_snapshot,
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
"""


class _NoClose:
    """Wraps sqlite3.Connection and suppresses close() so the in-memory DB
    survives across multiple get_connection() calls within a single test —
    mirrors the existing convention in tests/test_batch_profiling_columns_total.py
    and tests/test_phase5_profile_api.py."""

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
    """Fresh in-memory DB. Patches both data.domain_quality_service.get_connection
    and data.profiling_snapshot_resolver.get_connection: the resolver holds its
    own independently-imported connection reference, so patching only the
    consumer's would leave the resolver's queries hitting the real on-disk DB."""
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.domain_quality_service.get_connection", return_value=wrapper), \
         patch("data.profiling_snapshot_resolver.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
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
# 1-3, 5, 6, 7 — snapshot-selection regression guards
# ---------------------------------------------------------------------------

def test_selects_highest_snapshot_version(db):
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap1, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    snap2 = _seed_profiling_snapshot(db, src, snapshot_version=2, status="COMPLETE")
    _seed_table_profile(db, snap2, src, "dbo.edu_thing", "edu_thing", table_class="REPORTING")

    result = analyze_domain_quality(src, "user-1")

    # snap2's table_class (REPORTING -> Reporting & Analytics) must be the one
    # used, not snap1's (REFERENCE -> Reference Data).
    assert result["rules_analysis"][0]["generic_domain_spread"] == {"Reporting & Analytics": 1}


def test_older_complete_snapshot_wins_over_newer_cancelled(db):
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap1, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    # Newer CANCELLED snapshot — no table profile rows under it (mirrors a
    # real partial/cancelled run that never reached this table). The
    # resolver must never let this shadow the older, usable COMPLETE
    # snapshot (see data/profiling_snapshot_resolver.py policy).
    _seed_profiling_snapshot(db, src, snapshot_version=2, status="CANCELLED")

    result = analyze_domain_quality(src, "user-1")

    # The older COMPLETE snapshot (snap1) must win selection, so its
    # REFERENCE -> "Reference Data" profile data for dbo.edu_thing is used
    # even though a newer, empty CANCELLED snapshot exists.
    assert result["rules_analysis"][0]["generic_domain_spread"] == {"Reference Data": 1}
    assert result["rules_analysis"][0]["unique_generic_domains"] == 1


def test_older_complete_snapshot_wins_over_newer_running(db):
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")

    snap1 = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap1, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    # Newer RUNNING snapshot — no table profile rows under it yet. Must not
    # shadow the older, usable COMPLETE snapshot.
    _seed_profiling_snapshot(db, src, snapshot_version=2, status="RUNNING")

    result = analyze_domain_quality(src, "user-1")

    assert result["rules_analysis"][0]["generic_domain_spread"] == {"Reference Data": 1}
    assert result["rules_analysis"][0]["unique_generic_domains"] == 1


def test_source_scoping_unchanged(db):
    src_a = _seed_source(db, "user-A")
    src_b = _seed_source(db, "user-B")

    _seed_rule(db, src_a, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src_a, "dbo.a_thing", "Operations", 0.9, "PREFIX", "edu")
    snap_a = _seed_profiling_snapshot(db, src_a, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap_a, src_a, "dbo.a_thing", "a_thing", table_class="REFERENCE")

    # source B has a much higher snapshot_version — must not leak into A's result.
    _seed_rule(db, src_b, "PREFIX", "zz", "Finance")
    _seed_assignment(db, src_b, "dbo.b_thing", "Finance", 0.5, "PREFIX", "zz")
    snap_b = _seed_profiling_snapshot(db, src_b, snapshot_version=99, status="COMPLETE")
    _seed_table_profile(db, snap_b, src_b, "dbo.b_thing", "b_thing", table_class="REPORTING")

    result_a = analyze_domain_quality(src_a, "user-A")
    assert result_a["rules_analysis"][0]["pattern_value"] == "edu"
    assert result_a["rules_analysis"][0]["generic_domain_spread"] == {"Reference Data": 1}

    # user-A cannot read source owned by user-B
    assert analyze_domain_quality(src_b, "user-A") is None


def test_delegates_to_shared_resolver(db):
    """Proves the migration actually happened at runtime, not just in source."""
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    with patch(
        "data.domain_quality_service.get_latest_profiling_snapshot",
        wraps=_real_get_latest_profiling_snapshot,
    ) as spy:
        result = analyze_domain_quality(src, "user-1")

    spy.assert_called_once_with(src)
    assert result["rules_analysis"][0]["generic_domain_spread"] == {"Reference Data": 1}


# ---------------------------------------------------------------------------
# 4 — no-snapshot behavior unchanged
# ---------------------------------------------------------------------------

def test_no_snapshot_behavior_unchanged(db):
    """No profiling_snapshots row at all: the function does not special-case
    this (unlike get_column_profiles/get_table_profile_detail) — it simply
    proceeds with an empty profile_map, so every rule shows zero domain
    spread rather than the call failing or returning a distinct shape."""
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")

    result = analyze_domain_quality(src, "user-1")

    assert result is not None
    assert result["total_rules"] == 1
    assert result["rules_analysis"][0]["generic_domain_spread"] == {}
    assert result["rules_analysis"][0]["unique_generic_domains"] == 0


def test_unowned_source_returns_none(db):
    src = _seed_source(db, "user-1")
    assert analyze_domain_quality(src, "user-2") is None
    assert analyze_domain_quality(999999, "user-1") is None


# ---------------------------------------------------------------------------
# 8 — no writes
# ---------------------------------------------------------------------------

def test_makes_no_writes(db):
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_thing", "Operations", 0.9, "PREFIX", "edu")
    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap, src, "dbo.edu_thing", "edu_thing", table_class="REFERENCE")

    def _dump(table: str) -> list[dict]:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()]

    tables = (
        "data_source_connections", "domain_learning_rules", "domain_assignments",
        "profiling_snapshots", "profiling_table_profiles",
    )
    before = {t: _dump(t) for t in tables}

    analyze_domain_quality(src, "user-1")
    analyze_domain_quality(999999, "user-1")  # not-found path too

    after = {t: _dump(t) for t in tables}

    assert before == after


# ---------------------------------------------------------------------------
# 9, 10 — output structure and scoring/recommendations unchanged
# ---------------------------------------------------------------------------

def test_output_structure_and_scoring_for_healthy_rule(db):
    """Deterministic scenario: 2 tables matched by the 'edu' rule, mapped by
    the generic engine (via table_class alone, no keyword ambiguity) to two
    different domains — under the domain-spread threshold (3), so this rule
    is healthy and produces no recommendation.

    6 unrelated (no matching learned-rule evidence) assignments are also
    seeded so the 'edu' rule's share of all assigned tables (2/8 = 25%)
    stays at, not over, _THRESHOLD_SHARE_OF_ASSIGNED (0.25) — otherwise the
    share-of-assigned flag would fire regardless of domain spread, since
    with only 2 total assignments a single 2-table rule always covers 100%.
    """
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src, "dbo.edu_ref",  "Operations", 0.90, "PREFIX", "edu")
    _seed_assignment(db, src, "dbo.edu_rep",  "Operations", 0.85, "PREFIX", "edu")
    for i in range(6):
        db.execute(
            "INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence, evidence_json) "
            "VALUES (?, ?, 'Sales', 0.7, '[]')",
            (src, f"dbo.unrelated_{i}"),
        )
    db.commit()

    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap, src, "dbo.edu_ref", "edu_ref", table_class="REFERENCE")
    _seed_table_profile(db, snap, src, "dbo.edu_rep", "edu_rep", table_class="REPORTING")

    result = analyze_domain_quality(src, "user-1")

    assert set(result.keys()) == {
        "source_id", "total_rules", "total_assigned", "healthy_rules",
        "review_required_rules", "rules_analysis", "recommendations",
    }
    assert result["source_id"] == src
    assert result["total_rules"] == 1
    assert result["total_assigned"] == 8
    assert result["healthy_rules"] == 1
    assert result["review_required_rules"] == 0
    assert result["recommendations"] == []

    ra = result["rules_analysis"][0]
    assert ra["pattern_type"] == "PREFIX"
    assert ra["pattern_value"] == "edu"
    assert ra["rule_domain"] == "Operations"
    assert ra["tables_matched"] == 2
    assert ra["confidence_avg"] == pytest.approx(0.875)
    assert ra["generic_domain_spread"] == {"Reference Data": 1, "Reporting & Analytics": 1}
    assert ra["unique_generic_domains"] == 2


def test_flags_and_recommendation_for_domain_spread_rule(db):
    """4 tables matched, mapped to 4 distinct generic domains — over the
    domain-spread threshold (3) — must flag the rule and produce a
    recommendation."""
    src = _seed_source(db)
    _seed_rule(db, src, "PREFIX", "edu", "Operations")
    for fqn, name in [
        ("dbo.edu_ref", "edu_ref"),
        ("dbo.edu_rep", "edu_rep"),
        ("dbo.edu_payroll_history", "edu_payroll_history"),
        ("dbo.edu_student_enrollment", "edu_student_enrollment"),
    ]:
        _seed_assignment(db, src, fqn, "Operations", 0.8, "PREFIX", "edu")

    snap = _seed_profiling_snapshot(db, src, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap, src, "dbo.edu_ref", "edu_ref", table_class="REFERENCE")
    _seed_table_profile(db, snap, src, "dbo.edu_rep", "edu_rep", table_class="REPORTING")
    _seed_table_profile(db, snap, src, "dbo.edu_payroll_history", "edu_payroll_history")
    _seed_table_profile(db, snap, src, "dbo.edu_student_enrollment", "edu_student_enrollment")

    result = analyze_domain_quality(src, "user-1")

    ra = result["rules_analysis"][0]
    assert ra["tables_matched"] == 4
    assert ra["unique_generic_domains"] == 4
    assert result["healthy_rules"] == 0
    assert result["review_required_rules"] == 1
    assert len(result["recommendations"]) == 1
    rec = result["recommendations"][0]
    assert rec["rule_id"] == ra["rule_id"]
    assert "too broad" in rec["recommendation"]


# ---------------------------------------------------------------------------
# 11 — cross-source isolation of profiling data
# ---------------------------------------------------------------------------

def test_other_source_profiling_data_cannot_affect_result(db):
    """A different source's profiling_table_profiles rows for an
    identically-named table_fqn must never be picked up."""
    src_a = _seed_source(db, "user-A")
    src_b = _seed_source(db, "user-B")

    _seed_rule(db, src_a, "PREFIX", "edu", "Operations")
    _seed_assignment(db, src_a, "dbo.shared_name", "Operations", 0.9, "PREFIX", "edu")
    # source A has NO profiling snapshot at all.

    # source B has a snapshot with a same-named table, wrongly-scoped data
    # would leak in as "REFERENCE" if source scoping were broken.
    snap_b = _seed_profiling_snapshot(db, src_b, snapshot_version=1, status="COMPLETE")
    _seed_table_profile(db, snap_b, src_b, "dbo.shared_name", "shared_name", table_class="REFERENCE")

    result = analyze_domain_quality(src_a, "user-A")

    assert result["rules_analysis"][0]["generic_domain_spread"] == {}
    assert result["rules_analysis"][0]["unique_generic_domains"] == 0


# ---------------------------------------------------------------------------
# 12 — sibling domain-refinement consumer is now migrated too (Phase 2G.12)
# ---------------------------------------------------------------------------

def test_domain_refinement_service_now_migrated():
    """Phase 1D scope was exactly analyze_domain_quality(). The sibling
    domain_refinement_service.py's own inline profiling-snapshot lookup was
    unmigrated at that time; this replaces the prior "not migrated" guard
    now that Phase 2G.12 has migrated analyze_rule_refinement() onto the
    same shared resolver. Full migration-proof coverage (call count,
    conn=conn, AST checks, connection-lifecycle) lives in
    tests/test_domain_refinement_service.py - this is just a sibling-module
    sanity check."""
    with open("data/domain_refinement_service.py", encoding="utf-8") as f:
        source = f.read()

    assert "from data.profiling_snapshot_resolver import get_latest_profiling_snapshot" in source
    assert "FROM profiling_snapshots" not in source
