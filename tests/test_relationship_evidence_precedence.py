"""
Phase 6, Step 6B — structural view-dependency evidence trust boundary.

Defect 1 reproduction + fix coverage: data/relationship_service.py's
persist_relationships() used to be a plain INSERT OR IGNORE keyed on
(snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column) —
whichever writer recorded a given logical edge FIRST won permanently, even
when a later call carried strictly stronger evidence for the exact same
edge. This file proves that defect against the pre-fix behavior (see the
historical note in each test) and then proves the deterministic
evidence-precedence upsert fixes it, order-independently and idempotently,
without weakening any existing declared-FK guarantee.

Run from the project root:
    venv/Scripts/pytest tests/test_relationship_evidence_precedence.py -v
"""
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-rel-precedence-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-salt-rel-precedence-long-enough-1")

from core.sqlservice.retrieval_documents import classify_relationship_row
from data.relationship_service import (
    _EXISTING_RANK_SQL,
    _RANK_APPROVED,
    _RANK_CATALOG_VIEW_DEPENDENCY,
    _RANK_PHYSICAL_FK,
    _RANK_UNCONFIRMED,
    _evidence_rank,
    persist_relationships,
)

# ---------------------------------------------------------------------------
# Minimal schema — mirrors data/models.py's table_relationships DDL exactly
# (including the Program 3 Phase 1 / Step 6B extension columns this fix
# reads and writes).
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE table_relationships (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL,
        from_schema       TEXT    NOT NULL,
        from_table        TEXT    NOT NULL,
        from_table_fqn    TEXT    NOT NULL,
        from_column       TEXT    NOT NULL,
        to_schema         TEXT    NOT NULL,
        to_table          TEXT    NOT NULL,
        to_table_fqn      TEXT    NOT NULL,
        to_column         TEXT    NOT NULL,
        relationship_name TEXT,
        relationship_type TEXT    NOT NULL DEFAULT 'FOREIGN_KEY',
        confidence        REAL    NOT NULL DEFAULT 1.0,
        evidence_json     TEXT,
        created_at        TEXT    NOT NULL DEFAULT '2026-01-01',
        relationship_confidence INTEGER NOT NULL DEFAULT 100,
        inference_method  TEXT    NOT NULL DEFAULT 'declared_fk',
        relationship_status TEXT  NOT NULL DEFAULT 'AUTO',
        cardinality       TEXT    NOT NULL DEFAULT 'UNKNOWN',
        approved_by       TEXT,
        approved_at       TEXT
    );
    CREATE UNIQUE INDEX idx_tr_uniq ON table_relationships
        (snapshot_id, from_table_fqn, from_column, to_table_fqn, to_column);
"""


def _create_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _row(
    *,
    from_fqn: str = "dbo.vw_OpenTickets",
    from_col: str = "*",
    to_fqn: str = "dbo.Tickets",
    to_col: str = "*",
    relationship_type: str = "VIEW_DEPENDENCY",
    inference_method: str,
    relationship_status: str,
    confidence: float,
    source_id: int = 1,
    snapshot_id: int = 1,
    evidence_json: str = "{}",
) -> dict:
    f_schema, _, f_table = from_fqn.partition(".")
    t_schema, _, t_table = to_fqn.partition(".")
    return {
        "source_id": source_id, "snapshot_id": snapshot_id,
        "from_schema": f_schema, "from_table": f_table, "from_table_fqn": from_fqn,
        "from_column": from_col,
        "to_schema": t_schema, "to_table": t_table, "to_table_fqn": to_fqn,
        "to_column": to_col,
        "relationship_name": None, "relationship_type": relationship_type,
        "confidence": confidence, "evidence_json": evidence_json,
        "created_at": "2026-01-01T00:00:00+00:00",
        "inference_method": inference_method,
        "relationship_status": relationship_status,
    }


def _weak_regex_row(**kw) -> dict:
    """Shape of data/view_semantics_service.py's regex-derived evidence."""
    return _row(inference_method="view_dependency", relationship_status="PENDING", confidence=0.6, **kw)


def _strong_catalog_row(**kw) -> dict:
    """Shape of data/view_dependency_service.py's authoritative catalog evidence."""
    return _row(inference_method="catalog_view_dependency", relationship_status="AUTO", confidence=1.0, **kw)


def _fetch_one(conn, snapshot_id=1, from_fqn="dbo.vw_OpenTickets", from_col="*",
                to_fqn="dbo.Tickets", to_col="*"):
    row = conn.execute(
        "SELECT * FROM table_relationships WHERE snapshot_id=? AND from_table_fqn=? "
        "AND from_column=? AND to_table_fqn=? AND to_column=?",
        (snapshot_id, from_fqn, from_col, to_fqn, to_col),
    ).fetchone()
    assert row is not None, "expected exactly one row for this logical edge"
    return dict(row)


# ===========================================================================
# _evidence_rank — pure-function unit coverage, and Python/SQL parity
# ===========================================================================

def test_evidence_rank_orders_physical_fk_highest():
    assert _evidence_rank("FOREIGN_KEY", "declared_fk", "AUTO") == _RANK_PHYSICAL_FK
    assert _evidence_rank("VIEW_DEPENDENCY", "declared_fk", "AUTO") == _RANK_PHYSICAL_FK  # method alone suffices


def test_evidence_rank_orders_catalog_view_dependency_second():
    assert _evidence_rank("VIEW_DEPENDENCY", "catalog_view_dependency", "AUTO") == _RANK_CATALOG_VIEW_DEPENDENCY
    assert _RANK_CATALOG_VIEW_DEPENDENCY < _RANK_PHYSICAL_FK


def test_evidence_rank_orders_approved_third():
    assert _evidence_rank("INFERRED_NAME_MATCH", "name_match", "APPROVED") == _RANK_APPROVED
    assert _RANK_APPROVED < _RANK_CATALOG_VIEW_DEPENDENCY


def test_evidence_rank_unconfirmed_lowest():
    assert _evidence_rank("VIEW_DEPENDENCY", "view_dependency", "PENDING") == _RANK_UNCONFIRMED
    # Historical/legacy row shape: AUTO status but no strong inference_method
    # (predates the PENDING-writing fix) — must NOT rank as confirmed.
    assert _evidence_rank("VIEW_DEPENDENCY", "view_dependency", "AUTO") == _RANK_UNCONFIRMED
    assert _evidence_rank(None, None, None) == _RANK_UNCONFIRMED


def test_evidence_rank_matches_classify_relationship_row_confirmed_boundary():
    """rank >= 1 must be exactly classify_relationship_row's 'confirmed' set,
    and rank 0 must be exactly its 'inferred' set — the persistence-layer
    ranking is a refinement of the SAME policy, never a competing one."""
    matrix = [
        ("FOREIGN_KEY", "declared_fk", "AUTO"),
        ("VIEW_DEPENDENCY", "catalog_view_dependency", "AUTO"),
        ("INFERRED_NAME_MATCH", "name_match", "APPROVED"),
        ("VIEW_DEPENDENCY", "view_dependency", "PENDING"),
        ("VIEW_DEPENDENCY", "view_dependency", "AUTO"),
        ("INFERRED_VALUE_OVERLAP", None, "REJECTED"),
    ]
    for relationship_type, inference_method, relationship_status in matrix:
        row = {
            "relationship_type": relationship_type,
            "inference_method": inference_method,
            "relationship_status": relationship_status,
        }
        rank = _evidence_rank(relationship_type, inference_method, relationship_status)
        classification = classify_relationship_row(row)
        if rank >= 1:
            assert classification == "confirmed", (row, rank, classification)
        else:
            assert classification == "inferred", (row, rank, classification)


def test_evidence_rank_sql_matches_python(tmp_path):
    """The SQL CASE expression used inside persist_relationships()'s
    conditional UPDATE (_EXISTING_RANK_SQL) must agree with _evidence_rank()
    on every combination in the matrix below — this is the guard against the
    two ever silently drifting apart."""
    db = str(tmp_path / "rank_parity.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE t (relationship_type TEXT, inference_method TEXT, relationship_status TEXT)"
    )
    matrix = [
        ("FOREIGN_KEY", "declared_fk", "AUTO"),
        ("VIEW_DEPENDENCY", "declared_fk", "PENDING"),
        ("VIEW_DEPENDENCY", "catalog_view_dependency", "AUTO"),
        ("INFERRED_NAME_MATCH", "name_match", "APPROVED"),
        ("VIEW_DEPENDENCY", "view_dependency", "PENDING"),
        ("VIEW_DEPENDENCY", "view_dependency", "AUTO"),
        ("INFERRED_STRUCTURAL_PK_MATCH", None, "REJECTED"),
        (None, None, None),
    ]
    for relationship_type, inference_method, relationship_status in matrix:
        conn.execute("DELETE FROM t")
        conn.execute(
            "INSERT INTO t VALUES (?, ?, ?)",
            (relationship_type, inference_method, relationship_status),
        )
        sql_rank = conn.execute(f"SELECT {_EXISTING_RANK_SQL} FROM t").fetchone()[0]
        py_rank = _evidence_rank(relationship_type, inference_method, relationship_status)
        assert sql_rank == py_rank, (relationship_type, inference_method, relationship_status, sql_rank, py_rank)
    conn.close()


# ===========================================================================
# Defect 1 reproduction + fix — persist_relationships() precedence
# ===========================================================================

def test_weak_evidence_then_strong_evidence_upgrades(tmp_path):
    """Defect 1, direction 1: regex/PENDING evidence inserted FIRST must not
    block the later authoritative catalog/AUTO evidence from being
    correctly represented. Pre-fix (plain INSERT OR IGNORE), the second
    insert was silently discarded and the row stayed PENDING/view_dependency
    forever — this test fails against that old behavior for exactly that
    reason."""
    db = str(tmp_path / "upgrade.db")
    _create_db(db)
    conn = _db_conn(db)

    weak = _weak_regex_row()
    strong = _strong_catalog_row()

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[weak])
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[strong])

    row = _fetch_one(conn)
    assert row["inference_method"] == "catalog_view_dependency"
    assert row["relationship_status"] == "AUTO"
    assert row["confidence"] == 1.0
    assert classify_relationship_row(row) == "confirmed"

    # Exactly one physical row for this logical edge — no parallel duplicate.
    count = conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    assert count == 1
    conn.close()


def test_strong_evidence_then_weak_evidence_does_not_downgrade(tmp_path):
    """Defect 1, direction 2: catalog evidence inserted FIRST must never be
    downgraded by a later weaker (regex/PENDING) write for the same
    logical edge."""
    db = str(tmp_path / "no_downgrade.db")
    _create_db(db)
    conn = _db_conn(db)

    strong = _strong_catalog_row()
    weak = _weak_regex_row()

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[strong])
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[weak])

    row = _fetch_one(conn)
    assert row["inference_method"] == "catalog_view_dependency"
    assert row["relationship_status"] == "AUTO"
    assert row["confidence"] == 1.0
    assert classify_relationship_row(row) == "confirmed"

    count = conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    assert count == 1
    conn.close()


def test_equal_strength_rerun_is_idempotent(tmp_path):
    """Repeated writes of the identical evidence must not create a new row
    or change the existing one (rank tie -> no-op)."""
    db = str(tmp_path / "idempotent.db")
    _create_db(db)
    conn = _db_conn(db)

    strong = _strong_catalog_row()
    first = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[strong])
    before = _fetch_one(conn)

    second = persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[strong])
    after = _fetch_one(conn)

    assert first == 1
    assert second == 0
    assert before == after
    count = conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    assert count == 1
    conn.close()


def test_declared_fk_remains_executable_and_undowngradable(tmp_path):
    """A real declared FK (rank 3, the strongest tier) must never be
    downgraded by any weaker evidence for the same identity, including a
    same-rank-tier catalog_view_dependency claim."""
    db = str(tmp_path / "fk_stays.db")
    _create_db(db)
    conn = _db_conn(db)

    fk = _row(
        from_fqn="dbo.orders", from_col="customer_id",
        to_fqn="dbo.customers", to_col="id",
        relationship_type="FOREIGN_KEY", inference_method="declared_fk",
        relationship_status="AUTO", confidence=1.0,
    )
    weaker_claim = _row(
        from_fqn="dbo.orders", from_col="customer_id",
        to_fqn="dbo.customers", to_col="id",
        relationship_type="INFERRED_NAME_MATCH", inference_method="name_match",
        relationship_status="PENDING", confidence=0.4,
    )
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[fk])
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[weaker_claim])

    row = _fetch_one(conn, from_fqn="dbo.orders", from_col="customer_id",
                      to_fqn="dbo.customers", to_col="id")
    assert row["relationship_type"] == "FOREIGN_KEY"
    assert row["inference_method"] == "declared_fk"
    assert classify_relationship_row(row) == "confirmed"
    conn.close()


def test_catalog_view_dependency_persists_as_confirmed_lineage(tmp_path):
    """Sanity: a catalog-only write (no prior weak row at all) persists
    correctly and classifies as confirmed."""
    db = str(tmp_path / "catalog_only.db")
    _create_db(db)
    conn = _db_conn(db)

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_strong_catalog_row()])
    row = _fetch_one(conn)
    assert row["from_column"] == "*" and row["to_column"] == "*"
    assert classify_relationship_row(row) == "confirmed"
    conn.close()


def test_regex_view_dependency_remains_pending_when_no_catalog_evidence(tmp_path):
    """A regex-only write (no catalog confirmation ever arrives) must stay
    PENDING/inferred — never silently promoted."""
    db = str(tmp_path / "regex_only.db")
    _create_db(db)
    conn = _db_conn(db)

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_weak_regex_row()])
    row = _fetch_one(conn)
    assert row["relationship_status"] == "PENDING"
    assert classify_relationship_row(row) == "inferred"
    conn.close()


def test_no_duplicate_logical_edges_regardless_of_write_order(tmp_path):
    """Three writes for the exact same logical edge (weak, strong, weak
    again) must leave exactly one physical row, never three."""
    db = str(tmp_path / "no_dupes.db")
    _create_db(db)
    conn = _db_conn(db)

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_weak_regex_row()])
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_strong_catalog_row()])
    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_weak_regex_row()])

    count = conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    assert count == 1
    row = _fetch_one(conn)
    assert row["inference_method"] == "catalog_view_dependency"  # still the strongest evidence seen
    conn.close()


def test_historical_snapshot_scoping_preserved(tmp_path):
    """The SAME logical edge recorded across two DIFFERENT snapshot_ids
    (schema re-discovery) is intentional history, not a duplicate — both
    rows must be kept, independently."""
    db = str(tmp_path / "snapshot_scope.db")
    _create_db(db)
    conn = _db_conn(db)

    persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[_strong_catalog_row(snapshot_id=1)])
    persist_relationships(conn, snapshot_id=2, source_id=1, relationships=[_strong_catalog_row(snapshot_id=2)])

    count = conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0]
    assert count == 2
    snap_ids = {r["snapshot_id"] for r in conn.execute("SELECT snapshot_id FROM table_relationships")}
    assert snap_ids == {1, 2}
    conn.close()


def test_empty_list_returns_zero_and_untouched(tmp_path):
    db = str(tmp_path / "empty.db")
    _create_db(db)
    conn = _db_conn(db)
    assert persist_relationships(conn, snapshot_id=1, source_id=1, relationships=[]) == 0
    assert conn.execute("SELECT COUNT(*) FROM table_relationships").fetchone()[0] == 0
    conn.close()
