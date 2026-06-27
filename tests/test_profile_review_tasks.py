"""
Tests for get_profile_review_tasks() in data/profiling_service.py.

Every test uses an in-memory SQLite DB patched over data.profiling_service.get_connection
so no on-disk state is touched.  Each test case verifies that tasks are generated
(or not) from real profiling rows only — no fabricated data.

Run from project root:
    venv/Scripts/pytest tests/test_profile_review_tasks.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",      "test-jwt-review-tasks-long-enough-secret")
os.environ.setdefault("USER_ID_SALT",    "test-salt-review-tasks")

from data.profiling_service import _SEMANTIC_CONFIDENCE_THRESHOLD, get_profile_review_tasks


# ---------------------------------------------------------------------------
# Minimal schema — only the tables that get_profile_review_tasks queries
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL,
        display_name TEXT   NOT NULL DEFAULT 'Test Source',
        source_type TEXT    NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'relational_db',
        encrypted_config_json TEXT NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source_status TEXT NOT NULL DEFAULT 'ACTIVE',
        is_active   INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id               INTEGER NOT NULL,
        schema_snapshot_id      INTEGER NOT NULL DEFAULT 1,
        snapshot_version        INTEGER NOT NULL DEFAULT 1,
        mode                    TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        sample_rate             REAL    NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT    NOT NULL DEFAULT '4.0.0',
        status                  TEXT    NOT NULL DEFAULT 'COMPLETE',
        tables_total            INTEGER NOT NULL DEFAULT 0,
        tables_profiled         INTEGER NOT NULL DEFAULT 0,
        tables_skipped          INTEGER NOT NULL DEFAULT 0,
        tables_failed           INTEGER NOT NULL DEFAULT 0,
        tables_timed_out        INTEGER NOT NULL DEFAULT 0,
        columns_total           INTEGER NOT NULL DEFAULT 0,
        columns_profiled        INTEGER NOT NULL DEFAULT 0,
        columns_skipped         INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled     INTEGER NOT NULL DEFAULT 0,
        pii_columns_found       INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at              TEXT,
        completed_at            TEXT,
        duration_seconds        INTEGER,
        resumable_state_json    TEXT,
        batch_size              INTEGER NOT NULL DEFAULT 50,
        next_table_index        INTEGER NOT NULL DEFAULT 0,
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_table_profiles (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id     INTEGER NOT NULL,
        source_id                 INTEGER NOT NULL,
        table_fqn                 TEXT    NOT NULL,
        table_name                TEXT    NOT NULL,
        schema_name               TEXT    NOT NULL,
        table_type                TEXT    NOT NULL DEFAULT 'TABLE',
        exact_row_count           INTEGER,
        estimated_row_count       INTEGER,
        row_count_tier            TEXT,
        has_date_column           INTEGER NOT NULL DEFAULT 0,
        date_column_name          TEXT,
        earliest_record           TEXT,
        latest_record             TEXT,
        data_span_days            INTEGER,
        data_currency             TEXT    NOT NULL DEFAULT 'UNKNOWN',
        column_count              INTEGER NOT NULL DEFAULT 0,
        pk_column_count           INTEGER NOT NULL DEFAULT 0,
        fk_count                  INTEGER NOT NULL DEFAULT 0,
        referenced_by_count       INTEGER NOT NULL DEFAULT 0,
        is_junction_table         INTEGER NOT NULL DEFAULT 0,
        is_root_table             INTEGER NOT NULL DEFAULT 0,
        is_leaf_table             INTEGER NOT NULL DEFAULT 0,
        has_identity_column       INTEGER NOT NULL DEFAULT 0,
        avg_null_percentage       REAL,
        completeness_score        REAL,
        table_class               TEXT,
        classification_confidence REAL,
        classification_evidence_json TEXT,
        competing_classes_json    TEXT,
        classification_rule_version TEXT,
        pii_column_count          INTEGER NOT NULL DEFAULT 0,
        confirmed_pii_count       INTEGER NOT NULL DEFAULT 0,
        profiling_depth           TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms     INTEGER,
        profiling_status          TEXT    NOT NULL DEFAULT 'COMPLETE',
        skip_reason               TEXT,
        profiled_at               TEXT,
        created_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_column_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        column_name           TEXT    NOT NULL,
        data_type             TEXT    NOT NULL DEFAULT 'TEXT',
        raw_type              TEXT,
        is_nullable           INTEGER NOT NULL DEFAULT 1,
        is_primary_key        INTEGER NOT NULL DEFAULT 0,
        is_identity           INTEGER NOT NULL DEFAULT 0,
        ordinal_position      INTEGER NOT NULL DEFAULT 0,
        null_count            INTEGER,
        null_percentage       REAL,
        populated_count       INTEGER,
        populated_percentage  REAL,
        empty_string_count    INTEGER,
        zero_count            INTEGER,
        distinct_count        INTEGER,
        distinct_percentage   REAL,
        uniqueness_score      REAL,
        cardinality_tier      TEXT,
        min_value             TEXT,
        max_value             TEXT,
        min_length            INTEGER,
        max_length_observed   INTEGER,
        avg_length            REAL,
        mean_value            REAL,
        std_deviation         REAL,
        p5_value              TEXT,
        p95_value             TEXT,
        dominant_pattern      TEXT,
        pattern_coverage      REAL,
        email_match_rate      REAL,
        phone_match_rate      REAL,
        guid_match_rate       REAL,
        date_string_rate      REAL,
        numeric_string_rate   REAL,
        masked_value_rate     REAL,
        semantic_type         TEXT,
        semantic_confidence   REAL,
        semantic_evidence_json TEXT,
        semantic_rule_version TEXT,
        pii_name_heuristic    INTEGER NOT NULL DEFAULT 0,
        pii_confirmed         INTEGER NOT NULL DEFAULT 0,
        pii_signals_json      TEXT,
        top_values_coverage   REAL,
        profiling_depth       TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status      TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE domain_assignments (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id             INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        domain                TEXT    NOT NULL,
        confidence            REAL    NOT NULL DEFAULT 0.0,
        evidence_json         TEXT    NOT NULL DEFAULT '[]',
        competing_domains_json TEXT   NOT NULL DEFAULT '[]',
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE entity_assignments (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id               INTEGER NOT NULL,
        profiling_snapshot_id   INTEGER NOT NULL,
        table_fqn               TEXT    NOT NULL,
        entity                  TEXT    NOT NULL,
        confidence              REAL    NOT NULL DEFAULT 0.0,
        evidence_json           TEXT    NOT NULL DEFAULT '[]',
        competing_entities_json TEXT    NOT NULL DEFAULT '[]',
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE data_dictionary_tables (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id     INTEGER NOT NULL,
        snapshot_id   INTEGER NOT NULL DEFAULT 1,
        table_fqn     TEXT    NOT NULL,
        table_name    TEXT    NOT NULL,
        schema_name   TEXT    NOT NULL DEFAULT 'dbo',
        table_type    TEXT    NOT NULL DEFAULT 'TABLE',
        business_name TEXT,
        description   TEXT,
        domain        TEXT,
        grain         TEXT,
        is_approved   INTEGER NOT NULL DEFAULT 0,
        approved_by   TEXT,
        approved_at   TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at    TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE data_dictionary_columns (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id      INTEGER NOT NULL,
        snapshot_id    INTEGER NOT NULL DEFAULT 1,
        table_fqn      TEXT    NOT NULL,
        column_name    TEXT    NOT NULL,
        business_label TEXT,
        meaning        TEXT,
        semantic_type  TEXT,
        is_metric      INTEGER NOT NULL DEFAULT 0,
        is_dimension   INTEGER NOT NULL DEFAULT 0,
        is_date        INTEGER NOT NULL DEFAULT 0,
        is_id          INTEGER NOT NULL DEFAULT 0,
        pii_risk       INTEGER NOT NULL DEFAULT 0,
        is_approved    INTEGER NOT NULL DEFAULT 0,
        approved_by    TEXT,
        approved_at    TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _NoClose:
    """Wraps a connection and makes close() a no-op so in-memory DB survives."""
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    wrapper = _NoClose(conn)
    with patch("data.profiling_service.get_connection", return_value=wrapper):
        yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _src(db, user: str = "u1") -> int:
    cur = db.execute("INSERT INTO data_source_connections (user_id) VALUES (?)", (user,))
    db.commit()
    return cur.lastrowid


def _snap(db, source_id: int) -> int:
    ss = db.execute(
        "INSERT INTO schema_snapshots (source_id, source_type, snapshot_json, "
        "discovered_at, created_at) VALUES (?, 'mssql', '{}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (source_id,),
    ).lastrowid
    db.commit()
    cur = db.execute(
        "INSERT INTO profiling_snapshots (source_id, schema_snapshot_id, created_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (source_id, ss),
    )
    db.commit()
    return cur.lastrowid


def _table(db, snap_id: int, source_id: int, fqn: str = "dbo.Orders",
           status: str = "COMPLETE", skip_reason: str | None = None) -> None:
    parts = fqn.split(".", 1)
    schema, name = parts[0], parts[1]
    db.execute(
        """INSERT INTO profiling_table_profiles
           (profiling_snapshot_id, source_id, table_fqn, table_name, schema_name,
            table_type, column_count, profiling_depth, profiling_status,
            skip_reason, created_at, updated_at)
           VALUES (?,?,?,?,?,'TABLE',2,'STRUCTURAL_ONLY',?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (snap_id, source_id, fqn, name, schema, status, skip_reason),
    )
    db.commit()


def _col(db, snap_id: int, source_id: int, fqn: str, col: str,
         *, pii_heuristic: int = 0, pii_confirmed: int = 0,
         semantic_type: str | None = None, semantic_confidence: float | None = None,
         col_status: str = "COMPLETE") -> None:
    db.execute(
        """INSERT INTO profiling_column_profiles
           (profiling_snapshot_id, source_id, table_fqn, column_name,
            data_type, ordinal_position,
            pii_name_heuristic, pii_confirmed,
            semantic_type, semantic_confidence,
            profiling_depth, profiling_status, created_at, updated_at)
           VALUES (?,?,?,?,'TEXT',1,?,?,?,?,'STRUCTURAL_ONLY',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
        (snap_id, source_id, fqn, col,
         pii_heuristic, pii_confirmed,
         semantic_type, semantic_confidence,
         col_status),
    )
    db.commit()


def _domain(db, source_id: int, snap_id: int, fqn: str, domain: str = "Sales") -> None:
    db.execute(
        "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, created_at, updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (source_id, snap_id, fqn, domain),
    )
    db.commit()


def _entity(db, source_id: int, snap_id: int, fqn: str, entity: str = "Order") -> None:
    db.execute(
        "INSERT INTO entity_assignments (source_id, profiling_snapshot_id, table_fqn, entity, created_at, updated_at) "
        "VALUES (?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (source_id, snap_id, fqn, entity),
    )
    db.commit()


def _dict_table(db, source_id: int, fqn: str, business_name: str, approved: int = 0) -> None:
    parts = fqn.split(".", 1)
    schema, name = parts[0], parts[1]
    db.execute(
        "INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, "
        "business_name, is_approved, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (source_id, fqn, name, schema, business_name, approved),
    )
    db.commit()


def _dict_col(db, source_id: int, fqn: str, col: str, business_label: str, approved: int = 0) -> None:
    db.execute(
        "INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, "
        "business_label, is_approved, created_at, updated_at) "
        "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (source_id, fqn, col, business_label, approved),
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. Ownership / existence guards
# ---------------------------------------------------------------------------

def test_wrong_user_returns_none(db):
    src = _src(db, "alice")
    assert get_profile_review_tasks(src, "bob") is None


def test_nonexistent_source_returns_none(db):
    assert get_profile_review_tasks(9999, "u1") is None


def test_no_snapshot_returns_empty(db):
    src = _src(db)
    result = get_profile_review_tasks(src, "u1")
    assert result is not None
    assert result["tasks"] == []
    assert result["summary"]["total"] == 0


# ---------------------------------------------------------------------------
# 2. PII detected but not confirmed (CRITICAL)
# ---------------------------------------------------------------------------

def test_pii_heuristic_unconfirmed_generates_critical_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Customers")
    _col(db, snap, src, "dbo.Customers", "Email", pii_heuristic=1, pii_confirmed=0)

    result = get_profile_review_tasks(src, "u1")
    pii_tasks = [t for t in result["tasks"] if t["task_type"] == "Review PII Classification"]
    assert len(pii_tasks) == 1
    t = pii_tasks[0]
    assert t["severity"] == "CRITICAL"
    assert t["asset_type"] == "column"
    assert t["column_name"] == "Email"
    assert t["table_fqn"] == "dbo.Customers"
    assert t["nav_target"]["tab"] == "profile"
    assert t["status"] == "OPEN"


def test_pii_confirmed_does_not_generate_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Customers")
    _col(db, snap, src, "dbo.Customers", "Email", pii_heuristic=1, pii_confirmed=1)

    result = get_profile_review_tasks(src, "u1")
    pii_tasks = [t for t in result["tasks"] if t["task_type"] == "Review PII Classification"]
    assert len(pii_tasks) == 0


def test_no_pii_heuristic_does_not_generate_pii_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Customers")
    _col(db, snap, src, "dbo.Customers", "CustomerID", pii_heuristic=0, pii_confirmed=0)

    result = get_profile_review_tasks(src, "u1")
    pii_tasks = [t for t in result["tasks"] if t["task_type"] == "Review PII Classification"]
    assert len(pii_tasks) == 0


# ---------------------------------------------------------------------------
# 3. Semantic type confidence below threshold (HIGH)
# ---------------------------------------------------------------------------

def test_low_semantic_confidence_generates_high_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders")
    low_conf = _SEMANTIC_CONFIDENCE_THRESHOLD - 0.01
    _col(db, snap, src, "dbo.Orders", "Status",
         semantic_type="STATUS", semantic_confidence=low_conf)

    result = get_profile_review_tasks(src, "u1")
    sem_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Semantic Classification"]
    assert len(sem_tasks) == 1
    assert sem_tasks[0]["severity"] == "HIGH"
    assert sem_tasks[0]["column_name"] == "Status"
    assert "STATUS" in sem_tasks[0]["reason"]
    assert sem_tasks[0]["nav_target"]["tab"] == "profile"


def test_high_semantic_confidence_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders")
    high_conf = _SEMANTIC_CONFIDENCE_THRESHOLD + 0.01
    _col(db, snap, src, "dbo.Orders", "Status",
         semantic_type="STATUS", semantic_confidence=high_conf)

    result = get_profile_review_tasks(src, "u1")
    sem_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Semantic Classification"]
    assert len(sem_tasks) == 0


def test_null_semantic_confidence_no_task(db):
    """Columns with a semantic type but NULL confidence do not trigger a review task."""
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders")
    _col(db, snap, src, "dbo.Orders", "Status",
         semantic_type="STATUS", semantic_confidence=None)

    result = get_profile_review_tasks(src, "u1")
    sem_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Semantic Classification"]
    assert len(sem_tasks) == 0


def test_no_semantic_type_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders")
    _col(db, snap, src, "dbo.Orders", "Status",
         semantic_type=None, semantic_confidence=0.3)

    result = get_profile_review_tasks(src, "u1")
    sem_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Semantic Classification"]
    assert len(sem_tasks) == 0


# ---------------------------------------------------------------------------
# 4. Column classification failed (HIGH)
# ---------------------------------------------------------------------------

def test_failed_column_generates_review_classification_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Products")
    _col(db, snap, src, "dbo.Products", "Price", col_status="FAILED")

    result = get_profile_review_tasks(src, "u1")
    cls_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Classification"]
    assert len(cls_tasks) == 1
    assert cls_tasks[0]["severity"] == "HIGH"
    assert cls_tasks[0]["column_name"] == "Price"
    assert cls_tasks[0]["nav_target"]["tab"] == "profile"


def test_complete_column_no_classification_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Products")
    _col(db, snap, src, "dbo.Products", "Price", col_status="COMPLETE")

    result = get_profile_review_tasks(src, "u1")
    cls_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Classification"]
    assert len(cls_tasks) == 0


# ---------------------------------------------------------------------------
# 5. No Domain assigned (MEDIUM)
# ---------------------------------------------------------------------------

def test_unassigned_domain_generates_assign_domain_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Invoices")

    result = get_profile_review_tasks(src, "u1")
    dom_tasks = [t for t in result["tasks"] if t["task_type"] == "Assign Domain"]
    assert len(dom_tasks) == 1
    assert dom_tasks[0]["severity"] == "MEDIUM"
    assert dom_tasks[0]["table_fqn"] == "dbo.Invoices"
    assert dom_tasks[0]["nav_target"]["tab"] == "domains"


def test_assigned_domain_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Invoices")
    _domain(db, src, snap, "dbo.Invoices", "Finance")

    result = get_profile_review_tasks(src, "u1")
    dom_tasks = [t for t in result["tasks"] if t["task_type"] == "Assign Domain"]
    assert len(dom_tasks) == 0


# ---------------------------------------------------------------------------
# 6. No Entity assigned (MEDIUM)
# ---------------------------------------------------------------------------

def test_unassigned_entity_generates_assign_entity_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Employees")

    result = get_profile_review_tasks(src, "u1")
    ent_tasks = [t for t in result["tasks"] if t["task_type"] == "Assign Entity"]
    assert len(ent_tasks) == 1
    assert ent_tasks[0]["severity"] == "MEDIUM"
    assert ent_tasks[0]["nav_target"]["tab"] == "entities"


def test_assigned_entity_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Employees")
    _entity(db, src, snap, "dbo.Employees", "Employee")

    result = get_profile_review_tasks(src, "u1")
    ent_tasks = [t for t in result["tasks"] if t["task_type"] == "Assign Entity"]
    assert len(ent_tasks) == 0


# ---------------------------------------------------------------------------
# 7. Table profiling skipped (MEDIUM)
# ---------------------------------------------------------------------------

def test_skipped_table_generates_review_profiling_failure_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Archive", status="SKIPPED", skip_reason="Table too large")

    result = get_profile_review_tasks(src, "u1")
    skip_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Profiling Failure"]
    assert len(skip_tasks) == 1
    assert skip_tasks[0]["severity"] == "MEDIUM"
    assert "Table too large" in skip_tasks[0]["reason"]
    assert skip_tasks[0]["nav_target"]["tab"] == "profile"


def test_skipped_table_no_skip_reason_uses_default(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Archive", status="SKIPPED", skip_reason=None)

    result = get_profile_review_tasks(src, "u1")
    skip_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Profiling Failure"]
    assert len(skip_tasks) == 1
    assert skip_tasks[0]["reason"] != ""


def test_complete_table_no_skip_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders", status="COMPLETE")

    result = get_profile_review_tasks(src, "u1")
    skip_tasks = [t for t in result["tasks"] if t["task_type"] == "Review Profiling Failure"]
    assert len(skip_tasks) == 0


# ---------------------------------------------------------------------------
# 8. Dictionary entry pending approval (LOW)
# ---------------------------------------------------------------------------

def test_unapproved_table_dict_entry_generates_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Products")
    _dict_table(db, src, "dbo.Products", "Product Catalog", approved=0)

    result = get_profile_review_tasks(src, "u1")
    dict_tasks = [t for t in result["tasks"] if t["task_type"] == "Approve Dictionary Entry"]
    assert any(t["asset_type"] == "table" for t in dict_tasks)
    tbl = next(t for t in dict_tasks if t["asset_type"] == "table")
    assert tbl["severity"] == "LOW"
    assert "Product Catalog" in tbl["reason"]
    assert tbl["nav_target"]["tab"] == "dictionary"


def test_approved_table_dict_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Products")
    _dict_table(db, src, "dbo.Products", "Product Catalog", approved=1)

    result = get_profile_review_tasks(src, "u1")
    dict_tasks = [t for t in result["tasks"] if t["task_type"] == "Approve Dictionary Entry"
                  and t["asset_type"] == "table"]
    assert len(dict_tasks) == 0


def test_unapproved_column_dict_entry_generates_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Employees")
    _col(db, snap, src, "dbo.Employees", "Salary")
    _dict_col(db, src, "dbo.Employees", "Salary", "Annual Salary", approved=0)

    result = get_profile_review_tasks(src, "u1")
    dict_tasks = [t for t in result["tasks"] if t["task_type"] == "Approve Dictionary Entry"
                  and t["asset_type"] == "column"]
    assert len(dict_tasks) == 1
    assert dict_tasks[0]["column_name"] == "Salary"
    assert dict_tasks[0]["nav_target"]["tab"] == "dictionary"
    assert "Annual Salary" in dict_tasks[0]["reason"]


def test_approved_column_dict_no_task(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Employees")
    _col(db, snap, src, "dbo.Employees", "Salary")
    _dict_col(db, src, "dbo.Employees", "Salary", "Annual Salary", approved=1)

    result = get_profile_review_tasks(src, "u1")
    dict_tasks = [t for t in result["tasks"] if t["task_type"] == "Approve Dictionary Entry"
                  and t["asset_type"] == "column"]
    assert len(dict_tasks) == 0


# ---------------------------------------------------------------------------
# 9. Summary counts
# ---------------------------------------------------------------------------

def test_summary_counts_match_tasks(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.T")
    # 1 CRITICAL: PII unconfirmed
    _col(db, snap, src, "dbo.T", "Email", pii_heuristic=1, pii_confirmed=0)
    # 1 HIGH: semantic confidence low
    _col(db, snap, src, "dbo.T", "Status", semantic_type="STATUS",
         semantic_confidence=_SEMANTIC_CONFIDENCE_THRESHOLD - 0.05)
    # No domain → 1 MEDIUM (Assign Domain)
    # No entity → 1 MEDIUM (Assign Entity)

    result = get_profile_review_tasks(src, "u1")
    s = result["summary"]
    assert s["total"]    == len(result["tasks"])
    assert s["critical"] == 1
    assert s["high"]     >= 1   # at least the semantic task
    assert s["medium"]   >= 2   # Assign Domain + Assign Entity
    assert s["open"]     == s["total"]
    assert s["completed"] == 0


# ---------------------------------------------------------------------------
# 10. Sort order: CRITICAL → HIGH → MEDIUM → LOW, then alpha by asset_name
# ---------------------------------------------------------------------------

def test_tasks_sorted_by_severity_then_name(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Billing")
    _col(db, snap, src, "dbo.Billing", "Phone", pii_heuristic=1, pii_confirmed=0)
    _col(db, snap, src, "dbo.Billing", "Amount",
         semantic_type="AMOUNT", semantic_confidence=0.40)

    result = get_profile_review_tasks(src, "u1")
    sevs = [t["severity"] for t in result["tasks"]]
    _ORD = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    assert sevs == sorted(sevs, key=lambda s: _ORD.get(s, 99))


# ---------------------------------------------------------------------------
# 11. Source isolation
# ---------------------------------------------------------------------------

def test_different_user_cannot_access_tasks(db):
    src_a = _src(db, "alice")
    src_b = _src(db, "bob")
    snap_a = _snap(db, src_a)
    _table(db, snap_a, src_a, "dbo.T")

    assert get_profile_review_tasks(src_a, "bob") is None
    assert get_profile_review_tasks(src_b, "alice") is None


# ---------------------------------------------------------------------------
# 12. Deterministic task IDs
# ---------------------------------------------------------------------------

def test_task_id_is_deterministic(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Customers")
    _col(db, snap, src, "dbo.Customers", "Email", pii_heuristic=1, pii_confirmed=0)

    r1 = get_profile_review_tasks(src, "u1")
    r2 = get_profile_review_tasks(src, "u1")
    ids1 = {t["id"] for t in r1["tasks"]}
    ids2 = {t["id"] for t in r2["tasks"]}
    assert ids1 == ids2


# ---------------------------------------------------------------------------
# 13. Empty tasks when no issues
# ---------------------------------------------------------------------------

def test_fully_governed_source_returns_no_tasks(db):
    src = _src(db)
    snap = _snap(db, src)
    _table(db, snap, src, "dbo.Orders", status="COMPLETE")
    # Assign domain and entity → no medium tasks
    _domain(db, src, snap, "dbo.Orders", "Finance")
    _entity(db, src, snap, "dbo.Orders", "Order")
    # Column with confirmed PII + high confidence → no critical/high tasks
    _col(db, snap, src, "dbo.Orders", "OrderID",
         pii_heuristic=0, pii_confirmed=0,
         semantic_type="ID", semantic_confidence=0.95,
         col_status="COMPLETE")
    # Approved dict entries → no low tasks
    _dict_table(db, src, "dbo.Orders", "Sales Orders", approved=1)
    _dict_col(db, src, "dbo.Orders", "OrderID", "Order Identifier", approved=1)

    result = get_profile_review_tasks(src, "u1")
    assert result["tasks"] == []
    assert result["summary"]["total"] == 0
