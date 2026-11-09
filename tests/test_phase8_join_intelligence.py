"""
Tests for Program 3 Phase 2 — Enterprise Join Intelligence Engine.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern test_phase7 established —
avoids the hand-rolled-fixture drift that broke the older phase3-6 files.

Only data.semantic_layer_service.get_connection needs patching: the new
join-intelligence functions open one connection per call and pass it down
explicitly to every helper (relationship_service/profiling_service helpers
used here all take `conn` as an argument rather than opening their own).

Run from the project root:
    venv/Scripts/pytest tests/test_phase8_join_intelligence.py -v
"""
import os
import sqlite3
import time
from datetime import datetime, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase8-join-intel-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-phase8-salt-long-enough-value-1234567")

import data.models as models
from data.semantic_layer_service import (
    analyze_join_quality,
    discover_business_joins,
    discover_join_paths,
    detect_join_ambiguity,
    recommend_best_join_path,
)
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

_NOW = "2026-06-30T00:00:00+00:00"


def env(tmp_path, monkeypatch):
    """Build a fresh DB with the real schema, patch it in, seed source=1."""
    db_path = str(tmp_path / "phase8.db")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    monkeypatch.setattr(
        "data.semantic_layer_service.get_connection",
        lambda p=db_path: _db_conn(p),
    )

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


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _conn(db_path: str) -> sqlite3.Connection:
    return _db_conn(db_path)


_next_col_id = [100]


def _insert_table(db_path, table_fqn, *, table_class="Transactional", row_count=100):
    conn = _conn(db_path)
    tid = abs(hash(table_fqn)) % 1000000
    conn.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?, 'COMPLETE', ?, ?, ?)",
        (tid, table_fqn, table_fqn.split(".")[-1], table_fqn.split(".")[0],
         table_class, row_count, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_column(
    db_path, table_fqn, column_name, *,
    data_type="int", is_primary_key=0, is_identity=0,
    uniqueness_score=0.05, is_nullable=0, null_percentage=0.0,
):
    conn = _conn(db_path)
    _next_col_id[0] += 1
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,?)",
        (_next_col_id[0], table_fqn, column_name, data_type, is_primary_key, is_identity,
         uniqueness_score, is_nullable, null_percentage, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


_next_rel_id = [1000]


def _insert_relationship(
    db_path, from_fqn, from_col, to_fqn, to_col, *,
    relationship_type="FOREIGN_KEY", relationship_status="AUTO",
    confidence=1.0, relationship_confidence=100, inference_method="declared_fk",
):
    conn = _conn(db_path)
    _next_rel_id[0] += 1
    from_schema, from_table = from_fqn.split(".")
    to_schema, to_table = to_fqn.split(".")
    conn.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, "
        " relationship_confidence, inference_method, relationship_status) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_next_rel_id[0], from_schema, from_table, from_fqn, from_col,
         to_schema, to_table, to_fqn, to_col, f"FK_{_next_rel_id[0]}", relationship_type,
         confidence, "{}", _NOW,
         relationship_confidence, inference_method, relationship_status),
    )
    conn.commit()
    conn.close()
    return _next_rel_id[0]


# ---------------------------------------------------------------------------
# 1. Cardinality — ONE_TO_ONE
# ---------------------------------------------------------------------------

def test_cardinality_one_to_one(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.users", table_class="Master")
    _insert_table(db, "dbo.user_profiles", table_class="Master")
    _insert_column(db, "dbo.users", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.user_profiles", "user_id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.user_profiles", "user_id", "dbo.users", "id")

    result = analyze_join_quality(1, "u1", "dbo.user_profiles", "dbo.users")
    best = result["best_join"]
    assert best["cardinality"] == "ONE_TO_ONE"
    assert best["fanout_risk"] == "LOW"
    assert best["join_type"] == "INNER"


# ---------------------------------------------------------------------------
# 2. Cardinality — MANY_TO_ONE (and its reverse, ONE_TO_MANY)
# ---------------------------------------------------------------------------

def test_cardinality_many_to_one_and_one_to_many(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.customers", table_class="Master", row_count=100)
    _insert_table(db, "dbo.orders", table_class="Transactional", row_count=5000)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    # MANY_TO_ONE direction: orders (many) -> customers (one)
    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")
    best = result["best_join"]
    assert best["cardinality"] == "MANY_TO_ONE"
    assert best["fanout_risk"] == "LOW"
    assert best["driving_table"] == "dbo.orders"


# ---------------------------------------------------------------------------
# 3. Cardinality — MANY_TO_MANY, fan-out HIGH, join_type FULL
# ---------------------------------------------------------------------------

def test_cardinality_many_to_many_and_fanout_high(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.students", row_count=200)
    _insert_table(db, "dbo.courses", row_count=50)
    # Neither side is unique -> MANY_TO_MANY
    _insert_column(db, "dbo.students", "course_ref", uniqueness_score=0.1)
    _insert_column(db, "dbo.courses", "student_ref", uniqueness_score=0.1)
    _insert_relationship(db, "dbo.students", "course_ref", "dbo.courses", "student_ref")

    result = analyze_join_quality(1, "u1", "dbo.students", "dbo.courses")
    best = result["best_join"]
    assert best["cardinality"] == "MANY_TO_MANY"
    assert best["fanout_risk"] == "HIGH"
    assert best["join_type"] == "FULL"
    assert any("MANY_TO_MANY" in w for w in best["weaknesses"])


# ---------------------------------------------------------------------------
# 4. Fan-out escalation — ONE_TO_MANY with a large row-count ratio -> HIGH
# ---------------------------------------------------------------------------

def test_fanout_escalates_with_row_count_ratio(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.employees", table_class="Master", row_count=10)
    _insert_table(db, "dbo.tickets", table_class="Transactional", row_count=5000)
    _insert_column(db, "dbo.employees", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.tickets", "assignee_id", is_nullable=1, null_percentage=10.0, uniqueness_score=0.01)
    _insert_relationship(db, "dbo.tickets", "assignee_id", "dbo.employees", "id")

    # Joining the OTHER direction (employees -> tickets) is ONE_TO_MANY with ~500x ratio.
    from data.semantic_layer_service import _assess_fanout_risk, get_connection
    conn = get_connection()
    fanout = _assess_fanout_risk(conn, 1, 1, "ONE_TO_MANY", "dbo.employees", "dbo.tickets")
    conn.close()
    assert fanout["fanout_risk"] == "HIGH"
    assert "500" in fanout["explanation"] or "x the row count" in fanout["explanation"]

    # And the natural (many->one) direction stays LOW with a nullable FK -> LEFT join.
    result = analyze_join_quality(1, "u1", "dbo.tickets", "dbo.employees")
    best = result["best_join"]
    assert best["cardinality"] == "MANY_TO_ONE"
    assert best["fanout_risk"] == "LOW"
    assert best["join_type"] == "LEFT"


# ---------------------------------------------------------------------------
# 5 & 6. Multiple join paths + ranking prefers the cleaner/shorter path
# ---------------------------------------------------------------------------

def test_multiple_paths_ranked_direct_path_wins(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional", row_count=5000)
    _insert_table(db, "dbo.customers", table_class="Master", row_count=100)
    _insert_table(db, "dbo.order_items", table_class="Transactional", row_count=20000)

    _insert_column(db, "dbo.orders", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.order_items", "order_id", uniqueness_score=0.05)
    _insert_column(db, "dbo.order_items", "customer_id", uniqueness_score=0.3)

    # Direct, clean path: orders -> customers
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")
    # Indirect path: order_items -> orders, order_items -> customers (weaker, APPROVED inferred)
    _insert_relationship(db, "dbo.order_items", "order_id", "dbo.orders", "id")
    _insert_relationship(
        db, "dbo.order_items", "customer_id", "dbo.customers", "id",
        relationship_type="INFERRED_NAME_MATCH", relationship_status="APPROVED",
        confidence=0.55, relationship_confidence=55, inference_method="name_match",
    )

    result = recommend_best_join_path(1, "u1", "dbo.orders", "dbo.customers")
    assert result["total_paths_found"] == 2
    best = result["best_join_path"]
    assert best["path"] == ["dbo.orders", "dbo.customers"]
    assert best["hops"] == 1
    alt = result["alternative_paths"][0]
    assert alt["hops"] == 2
    assert best["avg_join_quality"] >= alt["avg_join_quality"]
    assert "join_quality" in result["why_best"] or "hop" in result["why_best"]


def test_same_table_and_no_path_cases(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders")
    _insert_table(db, "dbo.customers")

    same = recommend_best_join_path(1, "u1", "dbo.orders", "dbo.orders")
    assert same["best_join_path"]["hops"] == 0
    assert same["total_paths_found"] == 1

    none_found = recommend_best_join_path(1, "u1", "dbo.orders", "dbo.customers")
    assert none_found["total_paths_found"] == 0
    assert none_found["best_join_path"] is None


# ---------------------------------------------------------------------------
# 7 & 8. Weak relationships produce a low join_quality_tier
# ---------------------------------------------------------------------------

def test_weak_relationship_low_join_quality(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.a")
    _insert_table(db, "dbo.b")
    _insert_column(db, "dbo.a", "ref_id", uniqueness_score=0.05)
    _insert_column(db, "dbo.b", "id", uniqueness_score=0.3)  # NOT a confirmed key
    _insert_relationship(
        db, "dbo.a", "ref_id", "dbo.b", "id",
        relationship_type="INFERRED_NAME_MATCH", relationship_status="APPROVED",
        confidence=0.32, relationship_confidence=32, inference_method="name_match",
    )

    result = analyze_join_quality(1, "u1", "dbo.a", "dbo.b")
    best = result["best_join"]
    assert best["join_quality"] < 60
    assert best["join_quality_tier"] in ("LOW", "MEDIUM")
    assert best["relationship_strength"] == "WEAK"
    assert best["weaknesses"]  # at minimum: target not a confirmed key


# ---------------------------------------------------------------------------
# 9. Join explanations — why, fan-out warning, business meaning, confidence
# ---------------------------------------------------------------------------

def test_join_explanation_fields_present(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")
    # A second, weaker candidate edge to exercise why_not_alternatives.
    _insert_column(db, "dbo.orders", "alt_customer_id", uniqueness_score=0.02)
    _insert_relationship(
        db, "dbo.orders", "alt_customer_id", "dbo.customers", "id",
        relationship_type="INFERRED_NAME_MATCH", relationship_status="APPROVED",
        confidence=0.4, relationship_confidence=40, inference_method="name_match",
    )

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")
    assert result["why_best"]
    assert result["why_not_alternatives"]
    best = result["best_join"]
    assert best["business_explanation"]
    assert best["fanout_explanation"]
    assert isinstance(best["relationship_confidence"], int)


def test_no_direct_join_message(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders")
    _insert_table(db, "dbo.customers")

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")
    assert result["best_join"] is None
    assert "No direct trusted relationship" in result["message"]


# ---------------------------------------------------------------------------
# Governance is never bypassed — PENDING/REJECTED never participate
# ---------------------------------------------------------------------------

def test_pending_relationship_excluded_from_join_analysis(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders")
    _insert_table(db, "dbo.customers")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(
        db, "dbo.orders", "customer_id", "dbo.customers", "id",
        relationship_type="INFERRED_NAME_MATCH", relationship_status="PENDING",
        confidence=0.9, relationship_confidence=90, inference_method="name_match",
    )

    direct = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")
    assert direct["best_join"] is None

    path = recommend_best_join_path(1, "u1", "dbo.orders", "dbo.customers")
    assert path["total_paths_found"] == 0


def test_ownership_checks(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders")
    _insert_table(db, "dbo.customers")

    assert analyze_join_quality(1, "someone-else", "dbo.orders", "dbo.customers") is None
    assert recommend_best_join_path(1, "someone-else", "dbo.orders", "dbo.customers") is None
    assert analyze_join_quality(999, "u1", "dbo.orders", "dbo.customers") is None


# ---------------------------------------------------------------------------
# 10. Large schema — bounded performance, no O(N^2) blowup
# ---------------------------------------------------------------------------

def test_large_schema_performance(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.hub", table_class="Master", row_count=50)
    _insert_column(db, "dbo.hub", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)

    leaf_count = 20
    for i in range(leaf_count):
        fqn = f"dbo.leaf{i}"
        _insert_table(db, fqn, table_class="Transactional", row_count=1000)
        _insert_column(db, fqn, "hub_id", uniqueness_score=0.05)
        _insert_relationship(db, fqn, "hub_id", "dbo.hub", "id")

    start = time.monotonic()
    result = recommend_best_join_path(1, "u1", "dbo.leaf0", "dbo.leaf1")
    elapsed = time.monotonic() - start

    assert result["total_paths_found"] >= 1
    assert result["best_join_path"]["path"] == ["dbo.leaf0", "dbo.hub", "dbo.leaf1"]
    assert elapsed < 5.0, f"recommend_best_join_path took {elapsed:.2f}s on a {leaf_count}-table hub schema"


# ---------------------------------------------------------------------------
# Regression — existing Phase 6 functions are byte-for-byte unmodified
# ---------------------------------------------------------------------------

def test_existing_semantic_functions_unaffected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders")
    _insert_table(db, "dbo.customers")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    biz = discover_business_joins(1, "u1", "dbo.orders", "dbo.customers")
    assert biz["recommended_join"]["join_type"] == "INNER"  # still always INNER, unchanged

    paths = discover_join_paths(1, "u1", "dbo.orders", "dbo.customers")
    assert paths["total_paths_found"] == 1

    ambiguity = detect_join_ambiguity(1, "u1", "dbo.orders", "dbo.customers")
    assert ambiguity["is_clean"] is True


# ---------------------------------------------------------------------------
# Enterprise Implementation — Join Path Priority Fix
#
# recommend_best_join_path must prefer the shortest trusted path over a
# longer one, using confidence only as a tie-breaker among paths of equal
# hop count — not the other way around. Real-the reference database regression: a 2-hop route
# with one legitimately-approved 55-confidence STRUCTURAL_PK_NAME_MATCH edge
# was previously outranked by an irrelevant 6-hop route built entirely from
# 100-confidence edges, because avg_join_quality/avg_relationship_confidence
# were ranked ahead of hop count.
# ---------------------------------------------------------------------------

def _column_exists(db, table_fqn, column_name):
    conn = _db_conn(db)
    row = conn.execute(
        "SELECT 1 FROM profiling_column_profiles "
        "WHERE profiling_snapshot_id = 1 AND table_fqn = ? AND column_name = ?",
        (table_fqn, column_name),
    ).fetchone()
    conn.close()
    return row is not None


def _mk_fk_pair(db, from_fqn, from_col, to_fqn, to_col, **rel_kwargs):
    """FK column (low uniqueness) on from_fqn -> PK column (unique) on to_fqn.

    Tolerates the PK-side (table_fqn, column) already existing — several
    edges in these fixtures fan in to the same shared key column.
    """
    if not _column_exists(db, from_fqn, from_col):
        _insert_column(db, from_fqn, from_col, uniqueness_score=0.05)
    if not _column_exists(db, to_fqn, to_col):
        _insert_column(db, to_fqn, to_col, is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_relationship(db, from_fqn, from_col, to_fqn, to_col, **rel_kwargs)


def test_short_trusted_path_beats_long_high_confidence_path(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    for fqn in (
        "dbo.EDU_Enrollment_Tracking", "dbo.EDU_Track", "dbo.EDU_Course",
        "dbo.EDU_Hop1", "dbo.EDU_Hop2", "dbo.EDU_Hop3", "dbo.EDU_Hop4", "dbo.EDU_Hop5",
    ):
        _insert_table(db, fqn)

    # Trusted 2-hop path: one edge is an approved STRUCTURAL_PK_NAME_MATCH at
    # confidence 55 — must NOT be rejected (requirement: do not reject
    # STRUCTURAL_PK_NAME_MATCH edges merely for confidence 55).
    _mk_fk_pair(
        db, "dbo.EDU_Enrollment_Tracking", "TrackID", "dbo.EDU_Track", "TrackID",
        relationship_type="INFERRED_STRUCTURAL_PK_MATCH", relationship_status="APPROVED",
        confidence=0.55, relationship_confidence=55, inference_method="STRUCTURAL_PK_NAME_MATCH",
    )
    _mk_fk_pair(db, "dbo.EDU_Course", "TrackID", "dbo.EDU_Track", "TrackID")

    # Irrelevant 6-hop path, every edge trusted AUTO at full confidence.
    _mk_fk_pair(db, "dbo.EDU_Enrollment_Tracking", "ChainID", "dbo.EDU_Hop1", "ChainID")
    _mk_fk_pair(db, "dbo.EDU_Hop1", "Link2", "dbo.EDU_Hop2", "Link2")
    _mk_fk_pair(db, "dbo.EDU_Hop2", "Link3", "dbo.EDU_Hop3", "Link3")
    _mk_fk_pair(db, "dbo.EDU_Hop3", "Link4", "dbo.EDU_Hop4", "Link4")
    _mk_fk_pair(db, "dbo.EDU_Hop4", "Link5", "dbo.EDU_Hop5", "Link5")
    _mk_fk_pair(db, "dbo.EDU_Hop5", "CourseLink", "dbo.EDU_Course", "CourseLink")

    result = recommend_best_join_path(
        1, "u1", "dbo.EDU_Enrollment_Tracking", "dbo.EDU_Course", max_depth=6,
    )
    assert result["total_paths_found"] == 2

    best = result["best_join_path"]
    alt = result["alternative_paths"][0]

    # Precondition: this reproduces the historical bug — the 6-hop path
    # really does score higher on quality/confidence than the 2-hop path,
    # so a quality-first ranking would (wrongly) have picked it.
    assert alt["hops"] == 6
    assert alt["avg_relationship_confidence"] > best["avg_relationship_confidence"]
    assert alt["avg_join_quality"] > best["avg_join_quality"]

    # The fix: shortest trusted path wins regardless.
    assert best["hops"] == 2
    assert best["path"] == ["dbo.EDU_Enrollment_Tracking", "dbo.EDU_Track", "dbo.EDU_Course"]
    assert best["min_relationship_confidence"] == 55


def test_untrusted_pending_path_never_selected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_table(db, "dbo.src")
    _insert_table(db, "dbo.mid")
    _insert_table(db, "dbo.tgt")

    # Would be the shortest (1-hop) path if it counted — it must not, since
    # it was never approved.
    _mk_fk_pair(
        db, "dbo.src", "tgt_id", "dbo.tgt", "id",
        relationship_status="PENDING", confidence=0.90, relationship_confidence=90,
        inference_method="name_match", relationship_type="INFERRED_NAME_MATCH",
    )
    # Only trusted route: 2 hops via dbo.mid.
    _mk_fk_pair(db, "dbo.src", "mid_id", "dbo.mid", "id")
    _mk_fk_pair(db, "dbo.mid", "tgt_id", "dbo.tgt", "id")

    result = recommend_best_join_path(1, "u1", "dbo.src", "dbo.tgt")
    assert result["total_paths_found"] == 1
    assert result["best_join_path"]["hops"] == 2
    assert result["best_join_path"]["path"] == ["dbo.src", "dbo.mid", "dbo.tgt"]

    # A source/target pair connected only by a PENDING edge has no usable path at all.
    _insert_table(db, "dbo.only_pending_a")
    _insert_table(db, "dbo.only_pending_b")
    _mk_fk_pair(
        db, "dbo.only_pending_a", "b_id", "dbo.only_pending_b", "id",
        relationship_status="PENDING", confidence=0.95, relationship_confidence=95,
        inference_method="name_match", relationship_type="INFERRED_NAME_MATCH",
    )
    none_found = recommend_best_join_path(1, "u1", "dbo.only_pending_a", "dbo.only_pending_b")
    assert none_found["total_paths_found"] == 0
    assert none_found["best_join_path"] is None


def test_equal_hop_paths_prefer_lower_fanout_then_higher_confidence(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    for fqn in ("dbo.src", "dbo.mid_a", "dbo.mid_c", "dbo.tgt"):
        _insert_table(db, fqn, row_count=100)
    _insert_table(db, "dbo.mid_b", row_count=10)

    # via mid_a: both edges LOW fanout (MANY_TO_ONE), full confidence — best overall.
    _mk_fk_pair(db, "dbo.src", "a_id", "dbo.mid_a", "id")
    _mk_fk_pair(db, "dbo.tgt", "a_id", "dbo.mid_a", "id")

    # via mid_c: both edges LOW fanout, but the second edge is a weaker
    # (still trusted) 60-confidence edge — same fanout tier as mid_a, so
    # confidence must break the tie.
    _mk_fk_pair(db, "dbo.src", "c_id", "dbo.mid_c", "id")
    _mk_fk_pair(
        db, "dbo.tgt", "c_id", "dbo.mid_c", "id",
        relationship_status="APPROVED", confidence=0.60, relationship_confidence=60,
        inference_method="name_match", relationship_type="INFERRED_NAME_MATCH",
    )

    # via mid_b: first edge LOW fanout (src -> mid_b, MANY_TO_ONE). Second
    # edge mid_b -> tgt is declared the other way round (mid_b's unique side
    # references a non-unique tgt column) giving ONE_TO_MANY, and tgt has a
    # >=10x row count vs mid_b, so it's assessed HIGH fanout risk despite
    # full confidence.
    _mk_fk_pair(db, "dbo.src", "b_id", "dbo.mid_b", "id")
    _insert_table(db, "dbo.tgt", row_count=2000)
    _insert_column(db, "dbo.tgt", "mid_b_fanout_col", uniqueness_score=0.05)
    _insert_relationship(db, "dbo.mid_b", "id", "dbo.tgt", "mid_b_fanout_col")

    result = recommend_best_join_path(1, "u1", "dbo.src", "dbo.tgt", max_depth=3)
    assert result["total_paths_found"] == 3

    ranked_mids = [p["path"][1] for p in [result["best_join_path"], *result["alternative_paths"]]]
    assert ranked_mids == ["dbo.mid_a", "dbo.mid_c", "dbo.mid_b"]
    assert result["best_join_path"]["worst_fanout_risk"] == "LOW"
    assert result["alternative_paths"][-1]["worst_fanout_risk"] == "HIGH"


# ---------------------------------------------------------------------------
# Phase 2B.5 — analyze_join_quality() resolver-delegation regression tests
#
# analyze_join_quality() now resolves prof_snap_id via the shared
# profiling_snapshot_resolver.get_latest_profiling_snapshot() instead of the
# module-local _latest_profiling_snap_id() helper (still used by
# recommend_best_join_path and detect_join_ambiguity, unchanged).
#
# env()'s DB_PATH monkeypatch alone is sufficient for data.db.get_connection()
# callers to reach the isolated per-test file (confirmed: all 16 pre-existing
# tests in this file pass unmodified after the migration). These new tests
# additionally patch data.profiling_snapshot_resolver.get_connection
# explicitly, per this phase's instructions, so isolation does not rely on
# that side effect alone.
# ---------------------------------------------------------------------------

def env2(tmp_path, monkeypatch):
    """env() plus an explicit patch of the resolver's own connection boundary."""
    db = env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "data.profiling_snapshot_resolver.get_connection",
        lambda p=db: _db_conn(p),
    )
    return db


def _insert_profiling_snapshot(db_path, *, snap_id, snapshot_version, status, schema_snapshot_id=1):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, status, created_at) "
        "VALUES (?,1,?,?,?,?)",
        (snap_id, schema_snapshot_id, snapshot_version, status, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_column_for_snapshot(
    db_path, snap_id, table_fqn, column_name, *,
    data_type="int", is_primary_key=0, is_identity=0,
    uniqueness_score=0.05, is_nullable=0, null_percentage=0.0,
):
    conn = _db_conn(db_path)
    _next_col_id[0] += 1
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, is_nullable, null_percentage, "
        " created_at, updated_at) "
        "VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?)",
        (_next_col_id[0], snap_id, table_fqn, column_name, data_type, is_primary_key, is_identity,
         uniqueness_score, is_nullable, null_percentage, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_table_for_snapshot(
    db_path, snap_id, table_fqn, *,
    table_class="Transactional", row_count=100, profiling_status="COMPLETE",
):
    conn = _db_conn(db_path)
    tid = abs(hash((table_fqn, snap_id))) % 1000000
    conn.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, created_at, updated_at) "
        "VALUES (?,?,1,?,?,?,?,?,?,?,?)",
        (tid, snap_id, table_fqn, table_fqn.split(".")[-1], table_fqn.split(".")[0],
         table_class, profiling_status, row_count, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


# 1 — delegates_to_shared_resolver
def test_analyze_join_quality_delegates_to_shared_resolver(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    assert result["best_join"] is not None
    spy.assert_called_once_with(1)


# 2 — resolver_called_once_per_function_invocation
def test_analyze_join_quality_resolver_called_once_per_function_invocation(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")
        analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    # Exactly one resolver call per function invocation — no internal retry,
    # no per-edge or per-column-profile re-resolution.
    assert spy.call_count == 2


# 3 — latest_snapshot_version_wins
def test_analyze_join_quality_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)  # profiling_snapshots id=1, snapshot_version=1 (env() default)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    # v1: customers.id NOT a primary key -> pk_quality_score = 0 (weak-key weakness fires)
    _insert_column(db, "dbo.customers", "id", is_primary_key=0, uniqueness_score=0.3)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="COMPLETE")
    # v2: customers.id IS a primary key -> pk_quality_score = 100 (genuinely
    # sourced from profiling_column_profiles.is_primary_key)
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    # v2's profile must win: no "not a confirmed primary/unique key" weakness
    assert not any("primary/unique key" in w for w in best["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in best["evidence"])


# 4 — newer_cancelled_beats_older_complete
def test_analyze_join_quality_newer_cancelled_beats_older_complete(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)  # id=1, snapshot_version=1
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=0, uniqueness_score=0.3)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="CANCELLED")
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    # Status-independent selection preserved: newer CANCELLED (v2) still wins
    assert not any("primary/unique key" in w for w in best["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in best["evidence"])


# 5 — newer_running_beats_older_complete
def test_analyze_join_quality_newer_running_beats_older_complete(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)  # id=1, snapshot_version=1
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=0, uniqueness_score=0.3)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="RUNNING")
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    # Status-independent selection preserved: newer RUNNING (v2) still wins
    assert not any("primary/unique key" in w for w in best["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in best["evidence"])


# 6 — no_snapshot_behavior_preserved
def test_analyze_join_quality_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase2b5_none.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda p=db_path: _db_conn(p))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda p=db_path: _db_conn(p))

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
    # Deliberately no profiling_snapshots row for source_id=1
    conn.commit()
    conn.close()

    _insert_relationship(db_path, "dbo.orders", "customer_id", "dbo.customers", "id")

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    # profile=None fallback preserved: no column profiles were ever consulted
    assert best["cardinality"] == "UNKNOWN"
    assert best["fanout_risk"] == "MEDIUM"


# 7 — column_profile_cardinality_preserved
def test_analyze_join_quality_column_profile_cardinality_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.customers", table_class="Master", row_count=100)
    _insert_table(db, "dbo.orders", table_class="Transactional", row_count=5000)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    assert best["cardinality"] == "MANY_TO_ONE"


# 8 — fanout_risk_preserved
def test_analyze_join_quality_fanout_risk_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.students", row_count=200)
    _insert_table(db, "dbo.courses", row_count=50)
    _insert_column(db, "dbo.students", "course_ref", uniqueness_score=0.1)
    _insert_column(db, "dbo.courses", "student_ref", uniqueness_score=0.1)
    _insert_relationship(db, "dbo.students", "course_ref", "dbo.courses", "student_ref")

    result = analyze_join_quality(1, "u1", "dbo.students", "dbo.courses")

    assert result is not None
    best = result["best_join"]
    assert best["cardinality"] == "MANY_TO_MANY"
    assert best["fanout_risk"] == "HIGH"


# 9 — join_quality_score_preserved
def test_analyze_join_quality_join_quality_score_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.users", table_class="Master")
    _insert_table(db, "dbo.user_profiles", table_class="Master")
    _insert_column(db, "dbo.users", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.user_profiles", "user_id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.user_profiles", "user_id", "dbo.users", "id")

    result = analyze_join_quality(1, "u1", "dbo.user_profiles", "dbo.users")

    assert result is not None
    best = result["best_join"]
    # Full relationship confidence (100) + ONE_TO_ONE + governance-trusted +
    # pk_quality 100 -> deterministic high score, unaffected by the migration.
    assert best["join_quality"] >= 80
    assert best["join_quality_tier"] is not None


# 10 — driving_table_and_join_type_preserved
def test_analyze_join_quality_driving_table_and_join_type_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.customers", table_class="Master", row_count=100)
    _insert_table(db, "dbo.orders", table_class="Transactional", row_count=5000)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    best = result["best_join"]
    assert best["driving_table"] == "dbo.orders"
    assert best["join_type"] == "INNER"


# 11 — warnings_and_recommendations_preserved
def test_analyze_join_quality_warnings_and_recommendations_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.students", row_count=200)
    _insert_table(db, "dbo.courses", row_count=50)
    _insert_column(db, "dbo.students", "course_ref", uniqueness_score=0.1)
    _insert_column(db, "dbo.courses", "student_ref", uniqueness_score=0.1)
    _insert_relationship(db, "dbo.students", "course_ref", "dbo.courses", "student_ref")

    result = analyze_join_quality(1, "u1", "dbo.students", "dbo.courses")

    assert result is not None
    best = result["best_join"]
    assert any("MANY_TO_MANY" in w for w in best["weaknesses"])
    assert result["why_best"] is not None and "join_quality" in result["why_best"]
    assert result["why_not_alternatives"] == []  # only one direct edge -> no alternatives


# 12 — performs_no_writes
def test_analyze_join_quality_performs_no_writes(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_tables = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    before_columns = [dict(r) for r in conn.execute("SELECT * FROM profiling_column_profiles").fetchall()]
    conn.close()

    analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_tables = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    after_columns = [dict(r) for r in conn.execute("SELECT * FROM profiling_column_profiles").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_tables == after_tables
    assert before_columns == after_columns


# 13 — connection count check (documents the accepted Phase 2B.4 tradeoff;
# no timing assertions here)
def test_analyze_join_quality_connection_count(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _insert_table(db, "dbo.orders", table_class="Transactional")
    _insert_table(db, "dbo.customers", table_class="Master")
    _insert_column(db, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_relationship(db, "dbo.orders", "customer_id", "dbo.customers", "id")

    service_calls = {"count": 0}
    resolver_calls = {"count": 0}

    orig_service_get_connection = __import__("data.semantic_layer_service", fromlist=["get_connection"]).get_connection
    orig_resolver_get_connection = __import__("data.profiling_snapshot_resolver", fromlist=["get_connection"]).get_connection

    def counting_service_conn():
        service_calls["count"] += 1
        return _db_conn(db)

    def counting_resolver_conn():
        resolver_calls["count"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.semantic_layer_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = analyze_join_quality(1, "u1", "dbo.orders", "dbo.customers")

    assert result is not None
    # Documents the accepted Phase 2B.4 tradeoff: 1 service connection (still
    # owns everything else in the function) + 1 resolver connection (opened
    # and closed internally by get_latest_profiling_snapshot) = 2 total.
    assert service_calls["count"] == 1
    assert resolver_calls["count"] == 1
    assert service_calls["count"] + resolver_calls["count"] == 2


# ---------------------------------------------------------------------------
# Phase 2B.6 — recommend_best_join_path() resolver-delegation regression tests
#
# recommend_best_join_path() now resolves prof_snap_id via the shared
# profiling_snapshot_resolver.get_latest_profiling_snapshot() instead of the
# module-local _latest_profiling_snap_id() helper (still used by
# detect_join_ambiguity, unchanged). analyze_join_quality was migrated in
# Phase 2B.5; this is the last of the two planner-critical consumers.
#
# Chain: dbo.customers <- dbo.orders <- dbo.invoices <- dbo.payments
# (orders.customer_id -> customers.id, invoices.order_id -> orders.id,
#  payments.invoice_id -> invoices.id). No direct FK exists between
# customers and payments, so the only path recommend_best_join_path can
# find between them is the 3-hop indirect chain.
# ---------------------------------------------------------------------------

def _seed_indirect_chain(
    db_path, *,
    orders_id_is_pk=True, orders_id_uniqueness=1.0,
    customers_row_count=100, orders_row_count=2000,
    invoices_row_count=2000, payments_row_count=2000,
):
    _insert_table(db_path, "dbo.customers", table_class="Master", row_count=customers_row_count)
    _insert_table(db_path, "dbo.orders", table_class="Transactional", row_count=orders_row_count)
    _insert_table(db_path, "dbo.invoices", table_class="Transactional", row_count=invoices_row_count)
    _insert_table(db_path, "dbo.payments", table_class="Transactional", row_count=payments_row_count)
    _insert_column(db_path, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column(db_path, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db_path, "dbo.invoices", "order_id", uniqueness_score=0.02)
    _insert_column(
        db_path, "dbo.orders", "id",
        is_primary_key=1 if orders_id_is_pk else 0, uniqueness_score=orders_id_uniqueness,
    )
    _insert_column(db_path, "dbo.payments", "invoice_id", uniqueness_score=0.02)
    _insert_column(db_path, "dbo.invoices", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_relationship(db_path, "dbo.orders", "customer_id", "dbo.customers", "id")
    _insert_relationship(db_path, "dbo.invoices", "order_id", "dbo.orders", "id")
    _insert_relationship(db_path, "dbo.payments", "invoice_id", "dbo.invoices", "id")


# 1 — delegates_to_shared_resolver
def test_recommend_best_join_path_delegates_to_shared_resolver(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    assert result["best_join_path"] is not None
    spy.assert_called_once_with(1)


# 2 — resolver_called_once_per_function_invocation
def test_recommend_best_join_path_resolver_called_once_per_function_invocation(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    with patch(
        "data.semantic_layer_service.get_latest_profiling_snapshot",
        wraps=get_latest_profiling_snapshot,
    ) as spy:
        recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")
        recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    # Exactly one resolver call per function invocation, regardless of the
    # 3-edge path found — not once per edge.
    assert spy.call_count == 2


# 3 — latest_snapshot_version_wins
def test_recommend_best_join_path_latest_snapshot_version_wins(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)  # profiling_snapshots id=1, snapshot_version=1 (env() default)
    _seed_indirect_chain(db, orders_id_is_pk=False, orders_id_uniqueness=0.3)  # v1: orders.id weak

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="COMPLETE")
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "order_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "id", is_primary_key=1, uniqueness_score=1.0)  # v2: strong
    _insert_column_for_snapshot(db, 2, "dbo.payments", "invoice_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "id", is_primary_key=1, uniqueness_score=1.0)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    inv_to_orders = next(e for e in edges if e["from_table_fqn"] == "dbo.invoices" and e["to_table_fqn"] == "dbo.orders")
    # v2's profile (orders.id is_primary_key=1) must win over v1's (weak, 0.3 uniqueness)
    assert not any("primary/unique key" in w for w in inv_to_orders["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in inv_to_orders["evidence"])


# 4 — newer_cancelled_beats_older_complete
def test_recommend_best_join_path_newer_cancelled_beats_older_complete(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db, orders_id_is_pk=False, orders_id_uniqueness=0.3)

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="CANCELLED")
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "order_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.payments", "invoice_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "id", is_primary_key=1, uniqueness_score=1.0)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    inv_to_orders = next(e for e in edges if e["from_table_fqn"] == "dbo.invoices" and e["to_table_fqn"] == "dbo.orders")
    # Status-independent selection preserved: newer CANCELLED (v2) still wins
    assert not any("primary/unique key" in w for w in inv_to_orders["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in inv_to_orders["evidence"])


# 5 — newer_running_beats_older_complete
def test_recommend_best_join_path_newer_running_beats_older_complete(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db, orders_id_is_pk=False, orders_id_uniqueness=0.3)

    _insert_profiling_snapshot(db, snap_id=2, snapshot_version=2, status="RUNNING")
    _insert_column_for_snapshot(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.customers", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "order_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.orders", "id", is_primary_key=1, uniqueness_score=1.0)
    _insert_column_for_snapshot(db, 2, "dbo.payments", "invoice_id", uniqueness_score=0.02)
    _insert_column_for_snapshot(db, 2, "dbo.invoices", "id", is_primary_key=1, uniqueness_score=1.0)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    inv_to_orders = next(e for e in edges if e["from_table_fqn"] == "dbo.invoices" and e["to_table_fqn"] == "dbo.orders")
    # Status-independent selection preserved: newer RUNNING (v2) still wins
    assert not any("primary/unique key" in w for w in inv_to_orders["weaknesses"])
    assert any(e["signal"] == "pk_quality" and e["points"] == 15 for e in inv_to_orders["evidence"])


# 6 — no_snapshot_behavior_preserved
def test_recommend_best_join_path_no_snapshot_behavior_preserved(tmp_path, monkeypatch):
    db_path = str(tmp_path / "phase2b6_none.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.semantic_layer_service.get_connection", lambda p=db_path: _db_conn(p))
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", lambda p=db_path: _db_conn(p))

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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',4,'{}',?,?)",
        (_NOW, _NOW),
    )
    # Deliberately no profiling_snapshots row for source_id=1
    conn.commit()
    conn.close()

    _insert_relationship(db_path, "dbo.orders", "customer_id", "dbo.customers", "id")
    _insert_relationship(db_path, "dbo.invoices", "order_id", "dbo.orders", "id")
    _insert_relationship(db_path, "dbo.payments", "invoice_id", "dbo.invoices", "id")

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    best = result["best_join_path"]
    assert best["hops"] == 3
    # profile=None fallback preserved for every edge: no column profiles ever consulted
    assert all(e["cardinality"] == "UNKNOWN" for e in best["edges"])
    assert all(e["fanout_risk"] == "MEDIUM" for e in best["edges"])


# 7 — indirect_path_selection_preserved
def test_recommend_best_join_path_indirect_path_selection_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    assert result["total_paths_found"] == 1
    best = result["best_join_path"]
    assert best["hops"] == 3
    assert best["path"] == ["dbo.customers", "dbo.orders", "dbo.invoices", "dbo.payments"]


# 8 — path_length_and_edge_order_preserved
def test_recommend_best_join_path_path_length_and_edge_order_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    assert len(edges) == 3
    ordered_pairs = [(e["from_table_fqn"], e["to_table_fqn"]) for e in edges]
    assert ordered_pairs == [
        ("dbo.orders", "dbo.customers"),
        ("dbo.invoices", "dbo.orders"),
        ("dbo.payments", "dbo.invoices"),
    ]
    assert edges[0]["from_column"] == "customer_id" and edges[0]["to_column"] == "id"
    assert edges[1]["from_column"] == "order_id" and edges[1]["to_column"] == "id"
    assert edges[2]["from_column"] == "invoice_id" and edges[2]["to_column"] == "id"


# 9 — per_edge_cardinality_preserved
def test_recommend_best_join_path_per_edge_cardinality_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    assert all(e["cardinality"] == "MANY_TO_ONE" for e in edges)


# 10 — fanout_risk_preserved
def test_recommend_best_join_path_fanout_risk_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    edges = result["best_join_path"]["edges"]
    assert all(e["fanout_risk"] == "LOW" for e in edges)
    assert result["best_join_path"]["worst_fanout_risk"] == "LOW"


# 11 — aggregate_path_score_preserved
def test_recommend_best_join_path_aggregate_path_score_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    best = result["best_join_path"]
    # Deterministic given the seed: every edge is MANY_TO_ONE (15/20 card
    # points), governance-trusted (15), full confidence (30/30), PK-backed
    # target (15/15), both tables profiling_status=COMPLETE (5/5), no
    # dictionary/domain/entity data seeded (0+0) -> 80/100 per edge.
    assert best["avg_join_quality"] == 80.0
    assert best["avg_relationship_confidence"] == 100.0
    assert best["min_relationship_confidence"] == 100
    assert best["cardinality_score"] == 9  # 3 edges * MANY_TO_ONE rank (3)


# 12 — weakest_edge_preserved
def test_recommend_best_join_path_weakest_edge_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)
    # Weaken the middle edge (invoices -> orders) to a lower, still-trusted confidence.
    conn = _db_conn(db)
    conn.execute(
        "UPDATE table_relationships SET confidence = 0.55, relationship_confidence = 55 "
        "WHERE from_table_fqn = 'dbo.invoices' AND to_table_fqn = 'dbo.orders'"
    )
    conn.commit()
    conn.close()

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    best = result["best_join_path"]
    # The weakest edge's confidence must drive min_relationship_confidence
    # and pull avg_relationship_confidence below the all-strong baseline (100).
    assert best["min_relationship_confidence"] == 55
    assert best["avg_relationship_confidence"] < 100.0
    edges = best["edges"]
    weak_edge = next(e for e in edges if e["from_table_fqn"] == "dbo.invoices" and e["to_table_fqn"] == "dbo.orders")
    assert weak_edge["relationship_confidence"] == 55


# 13 — warnings_and_recommendations_preserved
def test_recommend_best_join_path_warnings_and_recommendations_preserved(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    assert result["why_best"] is not None
    assert "hop" in result["why_best"]
    assert result["alternative_paths"] == []  # only one path exists -> no alternatives
    edges = result["best_join_path"]["edges"]
    assert all(e["weaknesses"] == [] for e in edges)  # fully strong seed -> no weaknesses
    assert result["message"] == "1 join path(s) ranked."


# 14 — performs_no_writes
def test_recommend_best_join_path_performs_no_writes(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    conn = _db_conn(db)
    before_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    before_tables = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    before_columns = [dict(r) for r in conn.execute("SELECT * FROM profiling_column_profiles").fetchall()]
    conn.close()

    recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    conn = _db_conn(db)
    after_snapshots = [dict(r) for r in conn.execute("SELECT * FROM profiling_snapshots").fetchall()]
    after_tables = [dict(r) for r in conn.execute("SELECT * FROM profiling_table_profiles").fetchall()]
    after_columns = [dict(r) for r in conn.execute("SELECT * FROM profiling_column_profiles").fetchall()]
    conn.close()

    assert before_snapshots == after_snapshots
    assert before_tables == after_tables
    assert before_columns == after_columns


# 15 — connection_count_is_one_service_plus_one_resolver
class _SqlCountingConn:
    """Wraps a real sqlite3 connection; counts execute() calls against profiling_snapshots."""

    def __init__(self, real_conn, counters):
        self._real = real_conn
        self._counters = counters

    def execute(self, sql, *args, **kwargs):
        self._counters["total_sql"] += 1
        if "FROM profiling_snapshots" in sql:
            self._counters["snapshot_queries"] += 1
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_recommend_best_join_path_connection_count(tmp_path, monkeypatch):
    db = env2(tmp_path, monkeypatch)
    _seed_indirect_chain(db)

    counters = {"service_conn": 0, "resolver_conn": 0, "total_sql": 0, "snapshot_queries": 0}

    def counting_service_conn():
        counters["service_conn"] += 1
        return _SqlCountingConn(_db_conn(db), counters)

    def counting_resolver_conn():
        counters["resolver_conn"] += 1
        return _SqlCountingConn(_db_conn(db), counters)

    monkeypatch.setattr("data.semantic_layer_service.get_connection", counting_service_conn)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", counting_resolver_conn)

    result = recommend_best_join_path(1, "u1", "dbo.customers", "dbo.payments")

    assert result is not None
    # Documents the accepted Phase 2B.4/2B.5 tradeoff, now confirmed for the
    # second migrated function: 1 service connection + 1 resolver connection.
    assert counters["service_conn"] == 1
    assert counters["resolver_conn"] == 1
    assert counters["service_conn"] + counters["resolver_conn"] == 2
    # Exactly one snapshot-selection query for the whole invocation — not
    # repeated per path edge (3 edges were analyzed in this path).
    assert counters["snapshot_queries"] == 1
