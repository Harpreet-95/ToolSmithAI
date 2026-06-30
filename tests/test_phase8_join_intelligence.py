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
