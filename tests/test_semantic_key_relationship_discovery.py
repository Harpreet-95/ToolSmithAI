"""
Tests for Semantic Key-Role Relationship Discovery (Enterprise Implementation
— Automatic Relationship Discovery, Parts 2-3).

discover_relationship_candidates() only pairs columns whose normalized name
stems are IDENTICAL, or that already share a domain/entity assignment.
discover_structural_pk_candidates() only pairs an EXACT column-name match
against a declared primary key, and explicitly excludes bare generic PK
names ("ID"/"Key"/...). Neither path can discover a real relationship like
child.SessionID -> parent.ID when "parent" carries no domain/entity link to
the child table and its own PK is a bare generic word.

discover_semantic_key_candidates() closes that gap: Part 2 matches a source
column's stem against a CANDIDATE TABLE's own name (bounded via a reverse
token index, not a schema-wide cross product); Part 3 measures real,
live, single-JOIN referential coverage to validate the pair instead of
trusting the lexical hint alone.

Uses the same real-schema fixture pattern as
test_phase7_relationship_intelligence.py (data.models.init_db against a
per-test temp SQLite file).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-semantic-key-secret-long-enough-value-123")
os.environ.setdefault("USER_ID_SALT", "test-semantic-key-salt-long-enough-value-1234")

import data.models as models
from data.relationship_service import (
    MIN_SUGGEST_CONFIDENCE,
    _build_table_stem_index,
    _semantic_key_stem,
    _table_name_tokens,
    discover_semantic_key_candidates,
    get_auto_trusted_pending_relationship_ids,
)

_NOW = "2026-08-10T00:00:00+00:00"
_SOURCE_ID = 1
_SNAPSHOT_ID = 1


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "semantic_key.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)",
        (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _conn(db_path: str) -> sqlite3.Connection:
    return _db_conn(db_path)


def _insert_column(
    db_path, col_id, table_fqn, column_name, *,
    data_type="int", is_primary_key=0, is_identity=0, uniqueness_score=0.05,
):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?)",
        (col_id, table_fqn, column_name, data_type,
         is_primary_key, is_identity, uniqueness_score, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _relationships(db_path):
    conn = _conn(db_path)
    rows = conn.execute("SELECT * FROM table_relationships WHERE source_id=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _find(rows, from_col, to_table_fqn, to_col):
    return next(
        (r for r in rows if r["from_column"] == from_col
         and r["to_table_fqn"] == to_table_fqn and r["to_column"] == to_col),
        None,
    )


def _no_live_connection(monkeypatch):
    monkeypatch.setattr(
        "data.relationship_service._open_live_connection",
        lambda source_id, user_id: (None, None),
    )


def _fake_live_connection_with_coverage(monkeypatch, coverage: float, *, source_distinct=100):
    monkeypatch.setattr(
        "data.relationship_service._open_live_connection",
        lambda source_id, user_id: ("FAKE_CONN", "FAKE_BUILDER"),
    )
    matched = round(source_distinct * coverage)

    def _fake_measure(live_conn, builder, from_col, to_col):
        return (
            {
                "source_nonnull_count": source_distinct,
                "source_distinct_count": source_distinct,
                "matched_distinct_count": matched,
                "coverage": round(matched / source_distinct, 3),
            },
            True,
        )

    monkeypatch.setattr("data.relationship_service._measure_referential_coverage", _fake_measure)


# ---------------------------------------------------------------------------
# Part 2 — lexical helpers (pure, no DB)
# ---------------------------------------------------------------------------

def test_semantic_key_stem_strips_id_suffix():
    assert _semantic_key_stem("SessionID") == "session"
    assert _semantic_key_stem("InstructorID") == "instructor"
    assert _semantic_key_stem("ProgramID") == "program"


def test_semantic_key_stem_none_for_bare_generic_id():
    assert _semantic_key_stem("ID") is None
    assert _semantic_key_stem("Key") is None
    assert _semantic_key_stem("Id") is None


def test_table_name_tokens_singularized():
    assert _table_name_tokens("dbo.Sessions") == ["session"]
    assert _table_name_tokens("dbo.EDU_Sessions") == ["edu", "session"]


def test_table_stem_index_maps_token_to_table():
    index = _build_table_stem_index(["dbo.Sessions", "dbo.Widgets"])
    assert index["session"] == {"dbo.Sessions"}
    assert index["widget"] == {"dbo.Widgets"}
    assert "id" not in index  # neither table's own name is a generic id-word


# ---------------------------------------------------------------------------
# Part 2/3 — end-to-end discovery
# ---------------------------------------------------------------------------

def test_sessionid_to_bare_id_discovered_without_live_connection(tmp_path, monkeypatch):
    """The real-production-style gap: child.SessionID -> parent.ID, where 'parent'
    has no domain/entity link to the child and its own PK is a bare generic
    word. Should be proposed as a low-confidence PENDING candidate on
    structural evidence alone when no live connection is available — but
    NOT auto-trusted (no value evidence yet)."""
    db = env(tmp_path, monkeypatch)
    _no_live_connection(monkeypatch)

    _insert_column(db, 1, "dbo.Sessions", "ID", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.Enrollments", "SessionID", uniqueness_score=0.1)
    # Decoy: shares no lexical hint with anything else in the schema.
    _insert_column(db, 3, "dbo.Widgets", "ID", is_primary_key=1, uniqueness_score=1.0)

    result = discover_semantic_key_candidates(_SOURCE_ID, "u1")
    assert result is not None
    assert result["live_connection_available"] is False
    assert result["candidates_persisted"] == 1

    rows = _relationships(db)
    match = _find(rows, "SessionID", "dbo.Sessions", "ID")
    assert match is not None
    assert match["relationship_status"] == "PENDING"
    assert match["relationship_confidence"] < MIN_SUGGEST_CONFIDENCE + 25  # weak, structural-only

    evidence = json.loads(match["evidence_json"])
    assert evidence["weaknesses"]  # name mismatch recorded as a weakness — no coverage to offset it

    # No Widgets pairing should ever be generated — nothing else in the
    # schema has a 'widget' stem, so the reverse-index lookup finds nothing.
    assert _find(rows, "ID", "dbo.Widgets", "ID") is None
    assert all(r["to_table_fqn"] != "dbo.Widgets" for r in rows if r["from_column"] != "ID")

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID)
    assert match["id"] not in trusted


def test_sessionid_to_bare_id_auto_trusts_with_strong_live_coverage(tmp_path, monkeypatch):
    """Same shape as above, but with a live connection reporting 95%
    referential coverage — real, measured proof the values line up. This
    is exactly the additional evidence Part 3 exists to supply, and should
    be enough (no weaknesses, confidence above the auto-trust floor) for
    get_auto_trusted_pending_relationship_ids to accept it automatically,
    with no manual approval."""
    db = env(tmp_path, monkeypatch)
    _fake_live_connection_with_coverage(monkeypatch, 0.95)

    _insert_column(db, 1, "dbo.Sessions", "ID", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.Enrollments", "SessionID", uniqueness_score=0.1)

    result = discover_semantic_key_candidates(_SOURCE_ID, "u1")
    assert result["live_connection_available"] is True
    assert result["candidates_live_validated"] == 1
    assert result["candidates_persisted"] == 1

    rows = _relationships(db)
    match = _find(rows, "SessionID", "dbo.Sessions", "ID")
    assert match is not None
    assert match["inference_method"] == "semantic_key_role"

    evidence = json.loads(match["evidence_json"])
    assert evidence["weaknesses"] == []

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID)
    assert match["id"] in trusted


def test_weak_live_coverage_stays_pending_not_trusted(tmp_path, monkeypatch):
    """A candidate with a lexical table-name hint but near-zero real
    referential coverage (types match, PK target exists, but the values
    don't actually line up) — the 'bad same-type candidate' the milestone
    requires stays rejected from auto-trust regardless of the structural
    coincidence."""
    db = env(tmp_path, monkeypatch)
    _fake_live_connection_with_coverage(monkeypatch, 0.02)

    _insert_column(db, 1, "dbo.Sessions", "ID", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.Enrollments", "SessionID", uniqueness_score=0.1)

    result = discover_semantic_key_candidates(_SOURCE_ID, "u1")
    assert result["candidates_live_validated"] == 1

    rows = _relationships(db)
    match = _find(rows, "SessionID", "dbo.Sessions", "ID")
    assert match is not None
    assert match["relationship_status"] == "PENDING"

    evidence = json.loads(match["evidence_json"])
    assert any("coverage" in w.lower() for w in evidence["weaknesses"])

    trusted = get_auto_trusted_pending_relationship_ids(_SOURCE_ID, _SNAPSHOT_ID)
    assert match["id"] not in trusted


def test_existing_pair_from_other_pass_is_not_duplicated(tmp_path, monkeypatch):
    """A pair already persisted (e.g. by discover_relationship_candidates'
    exact-stem Pass 1, or a declared FK) must be skipped, never duplicated —
    the same unique-index dedup contract the other two discovery functions
    honor."""
    db = env(tmp_path, monkeypatch)
    _no_live_connection(monkeypatch)

    _insert_column(db, 1, "dbo.Sessions", "ID", is_primary_key=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.Enrollments", "SessionID", uniqueness_score=0.1)

    conn = _conn(db)
    conn.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status, inference_method) "
        "VALUES (99,1,1,'dbo','Enrollments','dbo.Enrollments','SessionID',"
        " 'dbo','Sessions','dbo.Sessions','ID','prior','INFERRED_NAME_MATCH',"
        " 0.55,'{}',?, 'PENDING','name_match')",
        (_NOW,),
    )
    conn.commit()
    conn.close()

    result = discover_semantic_key_candidates(_SOURCE_ID, "u1")
    assert result["candidates_persisted"] == 0
    assert result["candidates_skipped_existing"] == 1

    rows = _relationships(db)
    matches = [r for r in rows if r["from_column"] == "SessionID" and r["to_table_fqn"] == "dbo.Sessions"]
    assert len(matches) == 1  # no duplicate row


def test_too_few_columns_returns_empty_result(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _no_live_connection(monkeypatch)
    _insert_column(db, 1, "dbo.Sessions", "ID", is_primary_key=1, uniqueness_score=1.0)

    result = discover_semantic_key_candidates(_SOURCE_ID, "u1")
    assert result["candidates_persisted"] == 0
    assert _relationships(db) == []


def test_unknown_source_returns_none(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert discover_semantic_key_candidates(999, "u1") is None
    assert discover_semantic_key_candidates(_SOURCE_ID, "not-the-owner") is None
