"""
Tests for Phase 5 — Profile Column & Table Detail APIs.

Verifies two new service functions:
  - get_column_profiles  (GET /v1/sources/{id}/profile/columns)
  - get_table_profile_detail (GET /v1/sources/{id}/profile/tables/{fqn})

All tests use an in-memory SQLite database patched over data.db.get_connection
so they never touch the real toolsmith.db on disk.

Run from the project root:
    venv/Scripts/pytest tests/test_phase5_profile_api.py -v
"""

import os
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",      "test-jwt-secret-phase5-toolsmithai-long-enough")
os.environ.setdefault("USER_ID_SALT",    "test-salt-phase5")

from data.profiling_service import get_column_profiles, get_table_profile_detail


# ── Minimal schema (only the tables these service functions touch) ─────────────

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
        status                   TEXT    NOT NULL DEFAULT 'COMPLETE',
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
        top_values_coverage         REAL,
        completeness_score          REAL,
        format_consistency_score    REAL,
        valid_count                 INTEGER,
        invalid_count               INTEGER,
        invalid_percentage          REAL,
        validation_status           TEXT,
        quality_score               REAL,
        quality_grade               TEXT,
        quality_summary_json        TEXT,
        profiling_depth             TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms       INTEGER,
        profiling_status            TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at                  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at                  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
"""


# ── In-memory DB fixture ───────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


class _NoClose:
    """Wraps sqlite3.Connection and makes close() a no-op.

    The service opens and closes one connection per function call.  In tests
    each close() would destroy the in-memory DB; suppressing it lets multiple
    service calls within one test share the same live connection.
    """
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def db():
    """Yields a fresh in-memory DB.

    Patches data.profiling_service.get_connection (the already-bound name)
    to return a no-close wrapper around the in-memory connection.
    The underlying connection is closed once after the test completes.
    """
    conn = _make_db()
    wrapper = _NoClose(conn)
    with patch("data.profiling_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ── Seed helpers ───────────────────────────────────────────────────────────────

def _seed_source(db, user_id: str = "user-1") -> int:
    cur = db.execute(
        "INSERT INTO data_source_connections (user_id) VALUES (?)", (user_id,)
    )
    db.commit()
    return cur.lastrowid


def _seed_snapshot(db, source_id: int) -> int:
    cur = db.execute(
        "INSERT INTO schema_snapshots (source_id, source_type, snapshot_json, "
        "discovered_at, created_at) VALUES (?, 'mssql', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (source_id,),
    )
    db.commit()
    ss_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, created_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (source_id, ss_id),
    )
    db.commit()
    return cur.lastrowid


def _seed_table(db, snap_id: int, source_id: int, fqn: str = "dbo.students") -> None:
    parts = fqn.split(".", 1)
    schema, name = parts[0], parts[1]
    db.execute(
        """INSERT INTO profiling_table_profiles
           (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
            table_type, column_count, fk_count, referenced_by_count,
            table_class, classification_confidence,
            profiling_depth, profiling_status, created_at, updated_at)
           VALUES (?,?,?,?,?,'TABLE',3,2,5,'Master',0.87,
                   'STRUCTURAL_ONLY','COMPLETE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (snap_id, source_id, fqn, name, schema),
    )
    db.commit()


def _seed_column(
    db,
    snap_id: int,
    source_id: int,
    fqn: str,
    col_name: str,
    ordinal: int,
    *,
    semantic_type: str = "ID",
    pii: int = 0,
    data_type: str = "INTEGER",
) -> None:
    db.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            data_type, raw_type, is_nullable, is_primary_key, is_identity,
            ordinal_position, semantic_type, semantic_confidence,
            pii_name_heuristic, pii_confirmed,
            profiling_depth, profiling_status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,0,0,0,?,?,0.9,?,0,
                   'STRUCTURAL_ONLY','COMPLETE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (snap_id, source_id, fqn, col_name,
         data_type, data_type.lower(),
         ordinal, semantic_type, pii),
    )
    db.commit()


# ── 1. Ownership enforcement — get_column_profiles ───────────────────────────

def test_column_profiles_wrong_user_returns_none(db):
    src = _seed_source(db, "user-A")
    assert get_column_profiles(src, "user-B") is None


def test_column_profiles_nonexistent_source_returns_none(db):
    assert get_column_profiles(9999, "user-1") is None


# ── 2. Ownership enforcement — get_table_profile_detail ──────────────────────

def test_table_detail_wrong_user_returns_none(db):
    src = _seed_source(db, "user-A")
    assert get_table_profile_detail(src, "user-B", "dbo.students") is None


def test_table_detail_nonexistent_source_returns_none(db):
    assert get_table_profile_detail(9999, "user-1", "dbo.students") is None


# ── 3. No profiling snapshot yet ──────────────────────────────────────────────

def test_column_profiles_no_snapshot_returns_empty(db):
    src = _seed_source(db, "user-1")
    result = get_column_profiles(src, "user-1")
    assert result is not None
    assert result["snapshot_id"] is None
    assert result["total"] == 0
    assert result["columns"] == []


def test_table_detail_no_snapshot_returns_none_table(db):
    src = _seed_source(db, "user-1")
    result = get_table_profile_detail(src, "user-1", "dbo.students")
    assert result is not None
    assert result["table"] is None
    assert result["columns"] == []


# ── 4. Latest snapshot is used when multiple exist ────────────────────────────

def test_column_profiles_uses_latest_snapshot(db):
    src = _seed_source(db, "user-1")

    # Snapshot v1 — one column
    ss1 = db.execute(
        "INSERT INTO schema_snapshots (source_id, source_type, snapshot_json, "
        "discovered_at, created_at) VALUES (?, 'mssql', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (src,),
    ).lastrowid
    db.commit()
    snap1 = db.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (?, ?, 1, CURRENT_TIMESTAMP)",
        (src, ss1),
    ).lastrowid
    db.commit()
    _seed_column(db, snap1, src, "dbo.orders", "order_id", 1, semantic_type="ID")

    # Snapshot v2 — two columns; this is the one the service must use
    ss2 = db.execute(
        "INSERT INTO schema_snapshots (source_id, source_type, snapshot_json, "
        "discovered_at, created_at) VALUES (?, 'mssql', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (src,),
    ).lastrowid
    db.commit()
    snap2 = db.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (?, ?, 2, CURRENT_TIMESTAMP)",
        (src, ss2),
    ).lastrowid
    db.commit()
    _seed_column(db, snap2, src, "dbo.orders", "order_id",   1, semantic_type="ID")
    _seed_column(db, snap2, src, "dbo.orders", "order_date", 2, semantic_type="DATE", data_type="DATETIME")

    result = get_column_profiles(src, "user-1")
    assert result["snapshot_id"] == snap2
    assert result["total"] == 2


# ── 5. Column list returns stored profile rows ────────────────────────────────

def test_column_profiles_returns_all_columns(db):
    src = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.students", "student_id", 1, semantic_type="ID")
    _seed_column(db, snap, src, "dbo.students", "email",      2, semantic_type="EMAIL", pii=1, data_type="TEXT")
    _seed_column(db, snap, src, "dbo.students", "first_name", 3, semantic_type="NAME",  pii=1, data_type="TEXT")

    result = get_column_profiles(src, "user-1")
    assert result["total"] == 3
    assert len(result["columns"]) == 3


def test_column_profiles_response_shape(db):
    src = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.students", "student_id", 1, semantic_type="ID")

    result = get_column_profiles(src, "user-1")
    col = result["columns"][0]

    expected_keys = {
        "table_fqn", "column_name", "data_type", "raw_type",
        "is_nullable", "is_primary_key", "is_identity",
        "null_percentage", "distinct_percentage", "uniqueness_score", "cardinality_tier",
        "min_value", "max_value", "avg_length",
        "semantic_type", "semantic_confidence",
        "pii_name_heuristic", "pii_confirmed", "pii_signals_json",
        "dominant_pattern", "pattern_coverage", "top_values_coverage",
        "profiling_depth", "profiling_status",
    }
    assert expected_keys == set(col.keys())


def test_column_profiles_columns_ordered_by_ordinal(db):
    src = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    # Insert in reverse ordinal order
    _seed_column(db, snap, src, "dbo.t", "col_c", 3)
    _seed_column(db, snap, src, "dbo.t", "col_a", 1)
    _seed_column(db, snap, src, "dbo.t", "col_b", 2)

    result = get_column_profiles(src, "user-1")
    names = [c["column_name"] for c in result["columns"]]
    assert names == ["col_a", "col_b", "col_c"]


# ── 6. Filters work ───────────────────────────────────────────────────────────

def test_filter_by_table_fqn(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.students", "student_id", 1, semantic_type="ID")
    _seed_column(db, snap, src, "dbo.orders",   "order_id",   1, semantic_type="ID")

    result = get_column_profiles(src, "user-1", table_fqn="dbo.students")
    assert result["total"] == 1
    assert result["columns"][0]["table_fqn"] == "dbo.students"


def test_filter_by_semantic_type(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.t", "email",  1, semantic_type="EMAIL", pii=1, data_type="TEXT")
    _seed_column(db, snap, src, "dbo.t", "amount", 2, semantic_type="AMOUNT", data_type="DECIMAL")
    _seed_column(db, snap, src, "dbo.t", "status", 3, semantic_type="STATUS", data_type="TEXT")

    result = get_column_profiles(src, "user-1", semantic_type="EMAIL")
    assert result["total"] == 1
    assert result["columns"][0]["column_name"] == "email"


def test_filter_pii_only(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.t", "id",         1, semantic_type="ID",    pii=0)
    _seed_column(db, snap, src, "dbo.t", "email",      2, semantic_type="EMAIL", pii=1, data_type="TEXT")
    _seed_column(db, snap, src, "dbo.t", "first_name", 3, semantic_type="NAME",  pii=1, data_type="TEXT")

    result = get_column_profiles(src, "user-1", pii_only=True)
    assert result["total"] == 2
    assert all(c["pii_name_heuristic"] for c in result["columns"])


def test_filter_combined_table_and_pii(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_column(db, snap, src, "dbo.students", "email",  1, pii=1, data_type="TEXT")
    _seed_column(db, snap, src, "dbo.students", "id",     2, pii=0)
    _seed_column(db, snap, src, "dbo.orders",   "email2", 1, pii=1, data_type="TEXT")

    result = get_column_profiles(src, "user-1", table_fqn="dbo.students", pii_only=True)
    assert result["total"] == 1
    assert result["columns"][0]["column_name"] == "email"


# ── 7. Pagination ─────────────────────────────────────────────────────────────

def test_pagination_limit_and_offset(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    for i in range(1, 6):
        _seed_column(db, snap, src, "dbo.t", f"col_{i}", i)

    page1 = get_column_profiles(src, "user-1", limit=2, offset=0)
    assert page1["total"] == 5
    assert len(page1["columns"]) == 2
    assert page1["columns"][0]["column_name"] == "col_1"

    page2 = get_column_profiles(src, "user-1", limit=2, offset=2)
    assert len(page2["columns"]) == 2
    assert page2["columns"][0]["column_name"] == "col_3"

    page3 = get_column_profiles(src, "user-1", limit=2, offset=4)
    assert len(page3["columns"]) == 1
    assert page3["columns"][0]["column_name"] == "col_5"


def test_pagination_limit_clamped_to_500(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    result = get_column_profiles(src, "user-1", limit=9999)
    assert result["limit"] == 500


def test_pagination_limit_clamped_to_1_minimum(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    result = get_column_profiles(src, "user-1", limit=-5)
    assert result["limit"] == 1


# ── 8. Table detail returns table + columns ───────────────────────────────────

def test_table_detail_returns_table_and_columns(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.students")
    _seed_column(db, snap, src, "dbo.students", "student_id", 1, semantic_type="ID")
    _seed_column(db, snap, src, "dbo.students", "email",      2, semantic_type="EMAIL", pii=1, data_type="TEXT")

    result = get_table_profile_detail(src, "user-1", "dbo.students")
    assert result["table"] is not None
    assert result["table"]["table_fqn"] == "dbo.students"
    assert result["table"]["table_class"] == "Master"
    assert result["table"]["classification_confidence"] == pytest.approx(0.87)
    assert len(result["columns"]) == 2


def test_table_detail_columns_in_ordinal_order(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.t")
    _seed_column(db, snap, src, "dbo.t", "z_col", 3)
    _seed_column(db, snap, src, "dbo.t", "a_col", 1)
    _seed_column(db, snap, src, "dbo.t", "m_col", 2)

    result = get_table_profile_detail(src, "user-1", "dbo.t")
    names = [c["column_name"] for c in result["columns"]]
    assert names == ["a_col", "m_col", "z_col"]


def test_table_detail_table_shape(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.students")

    result  = get_table_profile_detail(src, "user-1", "dbo.students")
    t = result["table"]

    expected_keys = {
        "table_fqn", "table_name", "schema_name", "table_type",
        "exact_row_count", "estimated_row_count", "row_count_tier",
        "has_date_column", "date_column_name",
        "earliest_record", "latest_record", "data_span_days", "data_currency",
        "column_count", "pk_column_count", "fk_count", "referenced_by_count",
        "is_junction_table", "is_root_table", "is_leaf_table", "has_identity_column",
        "avg_null_percentage", "completeness_score",
        "table_class", "classification_confidence",
        "classification_evidence_json", "competing_classes_json",
        "pii_column_count", "confirmed_pii_count",
        "profiling_depth", "profiling_status", "profiled_at",
    }
    assert expected_keys == set(t.keys())


# ── 9. Table not profiled (wrong FQN) returns None table ─────────────────────

def test_table_detail_unknown_fqn_returns_none_table(db):
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.students")

    result = get_table_profile_detail(src, "user-1", "dbo.nonexistent")
    assert result["table"] is None
    assert result["columns"] == []


# ── 10. Source isolation — different users cannot cross-read ──────────────────

def test_two_sources_isolated_by_user(db):
    src_a = _seed_source(db, "user-A")
    src_b = _seed_source(db, "user-B")

    snap_a = _seed_snapshot(db, src_a)
    snap_b = _seed_snapshot(db, src_b)

    _seed_column(db, snap_a, src_a, "dbo.t", "col_a", 1)
    _seed_column(db, snap_b, src_b, "dbo.t", "col_b", 1)

    res_a = get_column_profiles(src_a, "user-A")
    res_b = get_column_profiles(src_b, "user-B")

    assert res_a["total"] == 1
    assert res_a["columns"][0]["column_name"] == "col_a"

    assert res_b["total"] == 1
    assert res_b["columns"][0]["column_name"] == "col_b"

    # user-A cannot read source owned by user-B
    assert get_column_profiles(src_b, "user-A") is None
    assert get_table_profile_detail(src_b, "user-A", "dbo.t") is None


# ── 11. Phase 1A/1B/1C deep-profiling fields in table detail column response ───

def _seed_column_deep(
    db,
    snap_id: int,
    source_id: int,
    fqn: str,
    col_name: str,
    ordinal: int,
    *,
    p5_value: str | None = None,
    p25_value: str | None = None,
    p50_value: str | None = None,
    p75_value: str | None = None,
    p95_value: str | None = None,
    histogram_json: str | None = None,
    distribution_shape: str | None = None,
    blank_percentage: float | None = None,
    completeness_score: float | None = None,
    format_consistency_score: float | None = None,
    invalid_count: int | None = None,
    invalid_percentage: float | None = None,
    quality_score: float | None = None,
    quality_grade: str | None = None,
    quality_summary_json: str | None = None,
) -> None:
    db.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            data_type, raw_type, is_nullable, is_primary_key, is_identity,
            ordinal_position, semantic_type, semantic_confidence,
            pii_name_heuristic, pii_confirmed,
            p5_value, p25_value, p50_value, p75_value, p95_value,
            histogram_json, distribution_shape, blank_percentage,
            completeness_score, format_consistency_score,
            invalid_count, invalid_percentage,
            quality_score, quality_grade, quality_summary_json,
            profiling_depth, profiling_status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,0,0,0,?,'ID',0.9,0,0,
                   ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   'STATISTICAL','COMPLETE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (snap_id, source_id, fqn, col_name,
         'INTEGER', 'integer', ordinal,
         p5_value, p25_value, p50_value, p75_value, p95_value,
         histogram_json, distribution_shape, blank_percentage,
         completeness_score, format_consistency_score,
         invalid_count, invalid_percentage,
         quality_score, quality_grade, quality_summary_json),
    )
    db.commit()


def test_table_detail_column_shape_includes_deep_fields(db):
    """All Phase 1A/1B/1C keys must be present in the column response, even when null."""
    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.sales")
    _seed_column(db, snap, src, "dbo.sales", "amount", 1)

    result = get_table_profile_detail(src, "user-1", "dbo.sales")
    col = result["columns"][0]

    deep_keys = {
        "p5_value", "p25_value", "p50_value", "p75_value", "p95_value",
        "histogram_json", "distribution_shape",
        "blank_percentage",
        "completeness_score", "format_consistency_score",
        "invalid_count", "invalid_percentage",
        "quality_score", "quality_grade", "quality_summary_json",
    }
    for key in deep_keys:
        assert key in col, f"Expected key '{key}' in table detail column response"


def test_table_detail_column_deep_fields_populated(db):
    """Stored Phase 1A/1B/1C values must be returned correctly by get_table_profile_detail."""
    import json as _json

    src  = _seed_source(db, "user-1")
    snap = _seed_snapshot(db, src)
    _seed_table(db, snap, src, "dbo.orders")

    hist = _json.dumps([{"lower_bound": 0.0, "upper_bound": 100.0, "row_count": 50, "percentage": 100.0}])
    summary = _json.dumps({"strengths": ["No nulls"], "issues": [], "recommendations": []})

    _seed_column_deep(
        db, snap, src, "dbo.orders", "total_amount", 1,
        p5_value="5.0", p25_value="25.0", p50_value="50.0",
        p75_value="75.0", p95_value="95.0",
        histogram_json=hist,
        distribution_shape="symmetric",
        blank_percentage=0.0,
        completeness_score=98.5,
        format_consistency_score=99.0,
        invalid_count=2,
        invalid_percentage=2.0,
        quality_score=97.5,
        quality_grade="A",
        quality_summary_json=summary,
    )

    result = get_table_profile_detail(src, "user-1", "dbo.orders")
    col = result["columns"][0]

    assert col["p5_value"]               == "5.0"
    assert col["p25_value"]              == "25.0"
    assert col["p50_value"]              == "50.0"
    assert col["p75_value"]              == "75.0"
    assert col["p95_value"]              == "95.0"
    assert col["distribution_shape"]     == "symmetric"
    assert col["blank_percentage"]       == pytest.approx(0.0)
    assert col["completeness_score"]     == pytest.approx(98.5)
    assert col["format_consistency_score"] == pytest.approx(99.0)
    assert col["invalid_count"]          == 2
    assert col["invalid_percentage"]     == pytest.approx(2.0)
    assert col["quality_score"]          == pytest.approx(97.5)
    assert col["quality_grade"]          == "A"
    hist_parsed = _json.loads(col["histogram_json"])
    assert len(hist_parsed) == 1
    assert hist_parsed[0]["row_count"] == 50
    summary_parsed = _json.loads(col["quality_summary_json"])
    assert "strengths" in summary_parsed
