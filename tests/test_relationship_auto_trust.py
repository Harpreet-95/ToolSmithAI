"""
Part 4 — data.relationship_service.get_auto_trusted_pending_relationship_ids.

Automatic evidence-based trust for PENDING relationships: never a status
mutation (no dry-run/live-write concern against the a real production database connection),
never requires human approval, never a blanket "trust every PENDING row."
Live-confirmed target: dbo.LMS_CLASS_SESSION_SECTIONS.InstructorID ->
dbo.EDU_Instructor.InstructorID (a row in a real production database) is PENDING via
STRUCTURAL_PK_NAME_MATCH (confidence=55, cardinality=MANY_TO_ONE, no
weaknesses) and competes with two decoy targets sharing the same column name
(dbo.EDU_Mentoring_ActiveInstructors, dbo.EDU_Mentoring_Active_Tier2_Instructors)
that tie on every score signal but have zero declared-FK graph connectivity.

Run:
    python -m pytest tests/test_relationship_auto_trust.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-relationship-auto-trust-secret-long-enough-12")
os.environ.setdefault("USER_ID_SALT", "test-relationship-auto-trust-salt-long-enough-123")

import data.models as models
from data.relationship_service import get_auto_trusted_pending_relationship_ids

_SOURCE_ID = 1
_SNAPSHOT_ID = 1


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _insert_relationship(
    conn, *, rel_id: int, from_table: str, from_col: str, to_table: str, to_col: str,
    status: str, confidence: int | None, cardinality: str | None,
    weaknesses: list | None = None,
) -> None:
    conn.execute(
        """INSERT INTO table_relationships
               (id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column,
                to_schema, to_table, to_table_fqn, to_column,
                relationship_name, relationship_type, confidence, evidence_json, created_at,
                relationship_confidence, inference_method, relationship_status, cardinality)
           VALUES (?, ?, ?, 'dbo', ?, ?, ?, 'dbo', ?, ?, ?, 'r', 'FK', ?, ?, '2026-01-01', ?, 'STRUCTURAL_PK_NAME_MATCH', ?, ?)""",
        (
            rel_id, _SOURCE_ID, _SNAPSHOT_ID,
            from_table.split(".")[-1], from_table, from_col,
            to_table.split(".")[-1], to_table, to_col,
            (confidence or 0) / 100.0, json.dumps({"weaknesses": weaknesses or []}),
            confidence, status, cardinality,
        ),
    )


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "reltrust.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda p=db_path: _db_conn(p))
    conn = _db_conn(db_path)
    conn.execute(
        """INSERT INTO data_source_connections
               (id, user_id, display_name, source_type, source_category,
                encrypted_config_json, config_schema_version, capabilities_json,
                metadata_json, source_status, is_active, created_at, updated_at)
           VALUES (?, 'user-1', 'Test Source', 'mssql', 'relational',
                   '{}', 1, '{}', '{}', 'connected', 1, '2026-01-01', '2026-01-01')""",
        (_SOURCE_ID,),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, discovered_at, created_at) "
        "VALUES (?, ?, 1, 'mssql', 0, '{}', '2026-01-01', '2026-01-01')",
        (_SNAPSHOT_ID, _SOURCE_ID),
    )
    conn.commit()
    return conn


def test_unambiguous_strong_pending_relationship_is_auto_trusted(tmp_path, monkeypatch):
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Instructor", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == {1}


def test_weak_confidence_pending_relationship_stays_blocked(tmp_path, monkeypatch):
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.A", from_col="XID",
        to_table="dbo.B", to_col="XID",
        status="PENDING", confidence=40, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == set()


def test_relationship_with_recorded_weakness_stays_blocked(tmp_path, monkeypatch):
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.A", from_col="XID",
        to_table="dbo.B", to_col="XID",
        status="PENDING", confidence=75, cardinality="MANY_TO_ONE",
        weaknesses=["Low sampled value overlap (10%) -- possible false positive."],
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == set()


def test_many_to_many_cardinality_stays_blocked_even_at_high_confidence(tmp_path, monkeypatch):
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.A", from_col="XID",
        to_table="dbo.B", to_col="XID",
        status="PENDING", confidence=90, cardinality="MANY_TO_MANY",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == set()


def test_decoy_competition_resolved_by_declared_relationship_degree(tmp_path, monkeypatch):
    """Reproduces the a real production database tie: three PENDING candidates share the same
    (from_table, from_column) and identical confidence/cardinality/evidence
    -- the winner must be the one whose target table has real declared
    (AUTO) FK connectivity, the two decoys must stay blocked."""
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Instructor", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    _insert_relationship(
        conn, rel_id=2, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Mentoring_ActiveInstructors", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    _insert_relationship(
        conn, rel_id=3, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Mentoring_Active_Tier2_Instructors", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    # Real declared FK connectivity for the correct target only.
    _insert_relationship(
        conn, rel_id=4, from_table="dbo.EDU_Instructor", from_col="MarketingID",
        to_table="dbo.EDU_Marketing", to_col="MarketingID",
        status="AUTO", confidence=100, cardinality="MANY_TO_ONE",
    )
    _insert_relationship(
        conn, rel_id=5, from_table="dbo.EDU_Instructor", from_col="CancelID",
        to_table="dbo.EDU_Cancel", to_col="CancelID",
        status="AUTO", confidence=100, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == {1}


def test_true_tie_with_no_degree_signal_stays_fully_blocked(tmp_path, monkeypatch):
    """When competing PENDING candidates ALSO tie on declared-relationship
    degree (e.g. both zero), none may auto-trust -- contested evidence
    remains blocked entirely, not resolved by arbitrary row order."""
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.A", from_col="XID",
        to_table="dbo.B", to_col="XID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    _insert_relationship(
        conn, rel_id=2, from_table="dbo.A", from_col="XID",
        to_table="dbo.C", to_col="XID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == set()


def test_promiscuous_source_table_stays_blocked_even_when_uncontested(tmp_path, monkeypatch):
    """Reproduces the live regression: dbo.EDU_ClassActive's bare
    ID-shaped columns each uncontestedly (no competing candidate) match a
    declared PK on a different, unrelated table -- individually each would
    pass score/cardinality/weakness checks, but the table's structural
    candidates span far more than _AUTO_TRUST_MAX_CANDIDATE_FANOUT distinct
    related tables, so none of them may auto-trust."""
    conn = env(tmp_path, monkeypatch)
    targets = ["dbo.A", "dbo.B", "dbo.C", "dbo.D", "dbo.E", "dbo.F", "dbo.G"]
    for i, target in enumerate(targets):
        _insert_relationship(
            conn, rel_id=i + 1, from_table="dbo.EDU_ClassActive", from_col=f"{target[4:]}ID",
            to_table=target, to_col=f"{target[4:]}ID",
            status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
        )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == set()


def test_focused_source_table_still_auto_trusts_under_the_fanout_cap(tmp_path, monkeypatch):
    """A source table with a SMALL, focused set of structural candidates
    (well under the fan-out cap) must still auto-trust normally -- the
    fan-out guard must not become a blanket new threshold that defeats the
    primary target."""
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Instructor", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    _insert_relationship(
        conn, rel_id=2, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="SectionID",
        to_table="dbo.EDU_Section", to_col="SectionID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == {1, 2}


def test_popular_target_referenced_by_many_sources_is_not_penalized(tmp_path, monkeypatch):
    """A widely-referenced TARGET table (many different source tables all
    pointing to the same real entity, e.g. dbo.EDU_Instructor referenced by
    several tables in a real production database) is a normal hub/dimension signal, not
    promiscuity -- the fan-out guard is outbound-only and must not
    penalize this. First draft of the guard (counting either direction)
    wrongly disqualified exactly this case."""
    conn = env(tmp_path, monkeypatch)
    sources = ["dbo.S1", "dbo.S2", "dbo.S3", "dbo.S4", "dbo.S5", "dbo.S6", "dbo.S7"]
    for i, src in enumerate(sources):
        _insert_relationship(
            conn, rel_id=i + 1, from_table=src, from_col="InstructorID",
            to_table="dbo.EDU_Instructor", to_col="InstructorID",
            status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
        )
    conn.commit()

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    assert trusted == {i + 1 for i in range(len(sources))}


def test_no_pending_rows_returns_empty_set(tmp_path, monkeypatch):
    conn = env(tmp_path, monkeypatch)
    assert get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn) == set()


def test_never_touches_relationship_status(tmp_path, monkeypatch):
    """Auto-trust is a read-time evaluation only -- the PENDING row's own
    relationship_status must remain untouched in the database."""
    conn = env(tmp_path, monkeypatch)
    _insert_relationship(
        conn, rel_id=1, from_table="dbo.LMS_CLASS_SESSION_SECTIONS", from_col="InstructorID",
        to_table="dbo.EDU_Instructor", to_col="InstructorID",
        status="PENDING", confidence=55, cardinality="MANY_TO_ONE",
    )
    conn.commit()

    get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID, conn=conn)

    row = conn.execute("SELECT relationship_status FROM table_relationships WHERE id = 1").fetchone()
    assert row["relationship_status"] == "PENDING"
