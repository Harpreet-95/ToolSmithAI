"""
Tests for Program 3 Phase 1 — Relationship Intelligence & Join Discovery Engine.

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file, rather than a hand-rolled minimal schema, so these tests
can never drift out of sync with the actual table_relationships columns the
way the older phase3-6 fixtures did (see Step 6 fixture fix).

Run from the project root:
    venv/Scripts/pytest tests/test_phase7_relationship_intelligence.py -v
"""
import ast
import inspect
import os
import sqlite3
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase7-relationship-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-phase7-salt-long-enough-value-1234567")

import data.models as models
from data.relationship_service import (
    MIN_SUGGEST_CONFIDENCE,
    approve_relationship,
    discover_relationship_candidates,
    explain_relationship,
    get_relationships_for_source,
    reject_relationship,
)
from data.governance_service import (
    BulkFilter,
    GovernedObjectType,
    GovernanceState,
    bulk_approve,
    bulk_reject,
    get_governance_profile,
)
from data.semantic_layer_service import discover_join_paths
from data.knowledge_graph_service import get_related_tables
from data.business_knowledge_service import get_table_business_context
from data.profiling_snapshot_resolver import get_latest_profiling_snapshot


# ---------------------------------------------------------------------------
# DB setup — full production schema via models.init_db(), patched onto a
# per-test temp file and wired into every consumer module's get_connection.
# ---------------------------------------------------------------------------

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.relationship_service",
    "data.governance_service",
    "data.semantic_layer_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
    # Phase 2F.3 — added ahead of the future migration of
    # discover_relationship_candidates() onto the shared resolver, so that
    # migration lands with test isolation already in place. The current
    # Relationship implementation does not call this module yet.
    "data.profiling_snapshot_resolver",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env(tmp_path, monkeypatch):
    """Build a fresh DB with the real schema, patch it into every module, seed source=1."""
    db_path = str(tmp_path / "phase7.db")

    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

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
    data_type="int", is_primary_key=0, is_identity=0,
    uniqueness_score=0.05, guid_match_rate=None, semantic_type=None,
    profiling_snapshot_id=1, source_id=1,
):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, guid_match_rate, semantic_type, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (col_id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type,
         is_primary_key, is_identity, uniqueness_score, guid_match_rate, semantic_type,
         _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_profiling_snapshot(db_path, snap_id, *, source_id, schema_snapshot_id, snapshot_version, status):
    """Phase 2F.3 helper — env() only ever seeds one hardcoded profiling_snapshots
    row (id=1); precedence/scoping/override tests need explicit control over
    additional rows' status and snapshot_version."""
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, status, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (snap_id, source_id, schema_snapshot_id, snapshot_version, status, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_value_samples(db_path, col_id, values):
    conn = _conn(db_path)
    for rank, v in enumerate(values):
        conn.execute(
            "INSERT INTO profiling_value_samples "
            "(profiling_column_profile_id, sample_type, value, rank) "
            "VALUES (?, 'TOP_VALUES', ?, ?)",
            (col_id, v, rank),
        )
    conn.commit()
    conn.close()


def _insert_dict_column(db_path, table_fqn, column_name, *, is_id=1):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, is_id, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, ?, ?, ?)",
        (table_fqn, column_name, is_id, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_domain(db_path, table_fqn, domain):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO domain_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, 0.9, ?, ?)",
        (table_fqn, domain, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_entity(db_path, table_fqn, entity):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, 0.9, ?, ?)",
        (table_fqn, entity, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_declared_fk(db_path, rel_id, from_fqn, from_col, to_fqn, to_col, fk_name="FK1"):
    conn = _conn(db_path)
    from_schema, from_table = from_fqn.split(".")
    to_schema, to_table = to_fqn.split(".")
    conn.execute(
        "INSERT INTO table_relationships "
        "(id, source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',1.0,?,?)",
        (rel_id, from_schema, from_table, from_fqn, from_col,
         to_schema, to_table, to_fqn, to_col, fk_name,
         '{"source":"schema_snapshot","snapshot_id":1,"fk_name":"%s"}' % fk_name, _NOW),
    )
    conn.commit()
    conn.close()


def _row(db_path, relationship_id):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT * FROM table_relationships WHERE id = ?", (relationship_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _pending_id(db_path):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT id FROM table_relationships WHERE relationship_status = 'PENDING'"
    ).fetchone()
    conn.close()
    return row["id"] if row else None


def _basic_name_match_pair(db_path):
    """orders.customer_id (FK-shaped, non-unique) -> customers.id (PK, unique)."""
    _insert_column(db_path, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db_path, 2, "dbo.orders", "customer_id", uniqueness_score=0.05)


# ---------------------------------------------------------------------------
# 1. Declared FK rows default to AUTO / declared_fk / confidence 100
# ---------------------------------------------------------------------------

def test_declared_fk_defaults(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")

    row = _row(db, 50)
    assert row["relationship_type"] == "FOREIGN_KEY"
    assert row["relationship_status"] == "AUTO"
    assert row["inference_method"] == "declared_fk"
    assert row["relationship_confidence"] == 100
    assert row["cardinality"] == "UNKNOWN"
    assert row["approved_by"] is None


# ---------------------------------------------------------------------------
# 2 & 3. discover_relationship_candidates creates PENDING name-match rows
# ---------------------------------------------------------------------------

def test_discover_creates_pending_name_match(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    result = discover_relationship_candidates(1, "u1")
    assert result["candidates_persisted"] == 1

    rels = get_relationships_for_source(1, "u1")
    assert len(rels) == 1
    row = _row(db, rels[0]["id"])
    assert row["relationship_status"] == "PENDING"
    assert row["relationship_type"] == "INFERRED_NAME_MATCH"
    assert row["inference_method"] == "name_match"
    assert 0 < row["relationship_confidence"] <= 100


# ---------------------------------------------------------------------------
# 4. Business-key inference (dictionary + domain + entity evidence only)
# ---------------------------------------------------------------------------

def test_business_key_inference(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # cust_ref / id do NOT share a normalized name stem ("cust" vs "customer")
    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.orders", "cust_ref", uniqueness_score=0.05, semantic_type="identifier")

    _insert_domain(db, "dbo.customers", "Sales")
    _insert_domain(db, "dbo.orders", "Sales")
    _insert_entity(db, "dbo.customers", "Customer")
    _insert_entity(db, "dbo.orders", "Customer")
    _insert_dict_column(db, "dbo.customers", "id", is_id=1)
    _insert_dict_column(db, "dbo.orders", "cust_ref", is_id=1)

    result = discover_relationship_candidates(1, "u1")
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    assert row["relationship_type"] == "INFERRED_BUSINESS_KEY"
    assert row["inference_method"] == "business_key"

    explanation = explain_relationship(1, "u1", rel_id)
    signals = {e["signal"] for e in explanation["evidence"]}
    assert "same_domain" in signals
    assert "same_entity" in signals
    assert "dictionary_id_match" in signals
    assert "name_match" not in signals


# ---------------------------------------------------------------------------
# 5. Value-overlap inference using existing profiling samples
# ---------------------------------------------------------------------------

def test_value_overlap_inference(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.orders", "cust_ref", uniqueness_score=0.05, semantic_type="identifier")
    _insert_domain(db, "dbo.customers", "Sales")
    _insert_domain(db, "dbo.orders", "Sales")

    # 100% of orders.cust_ref sample values also appear in customers.id samples
    _insert_value_samples(db, 1, ["1", "2", "3", "4", "5", "6", "7"])
    _insert_value_samples(db, 2, ["1", "2", "3", "4", "5"])

    result = discover_relationship_candidates(1, "u1")
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    assert row["relationship_type"] == "INFERRED_VALUE_OVERLAP"
    assert row["inference_method"] == "value_overlap"

    explanation = explain_relationship(1, "u1", rel_id)
    overlap_evidence = [e for e in explanation["evidence"] if e["signal"] == "value_overlap"]
    assert overlap_evidence and overlap_evidence[0]["points"] > 0
    assert not any("could not be verified" in w for w in explanation["weaknesses"])


# ---------------------------------------------------------------------------
# 6. Low-confidence candidates are discarded, not persisted
# ---------------------------------------------------------------------------

def test_low_confidence_discarded(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    # Cross-bucket pair joined only by a shared domain; neither side looks
    # like a primary key, no dictionary/entity signal, no value samples.
    # Score = datatype(10) + domain(10) = 20, below MIN_SUGGEST_CONFIDENCE.
    _insert_column(db, 1, "dbo.customers", "weak_a", uniqueness_score=0.1, semantic_type="identifier")
    _insert_column(db, 2, "dbo.orders", "weak_b", uniqueness_score=0.1, semantic_type="identifier")
    _insert_domain(db, "dbo.customers", "Ops")
    _insert_domain(db, "dbo.orders", "Ops")

    result = discover_relationship_candidates(1, "u1")
    assert result["candidates_evaluated"] == 1
    assert result["candidates_discarded_low_confidence"] == 1
    assert result["candidates_persisted"] == 0
    assert MIN_SUGGEST_CONFIDENCE == 30

    rels = get_relationships_for_source(1, "u1")
    assert rels == []


# ---------------------------------------------------------------------------
# 7. discover_relationship_candidates is idempotent
# ---------------------------------------------------------------------------

def test_discover_is_idempotent(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    first = discover_relationship_candidates(1, "u1")
    assert first["candidates_persisted"] == 1

    second = discover_relationship_candidates(1, "u1")
    assert second["candidates_persisted"] == 0
    assert second["candidates_skipped_existing"] == 1

    rels = get_relationships_for_source(1, "u1")
    assert len(rels) == 1


# ---------------------------------------------------------------------------
# 7b. discover_relationship_candidates records an audit event on every call
# ---------------------------------------------------------------------------

def test_discover_writes_audit_event(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    discover_relationship_candidates(1, "u1")

    conn = _conn(db)
    rows = conn.execute(
        "SELECT task_type, status, user_id FROM audit_logs "
        "WHERE task_type = 'relationship_candidate_discovery'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["user_id"] == "u1"


# ---------------------------------------------------------------------------
# 8 & 9. explain_relationship — declared FK and inferred
# ---------------------------------------------------------------------------

def test_explain_declared_fk(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")

    explanation = explain_relationship(1, "u1", 50)
    assert explanation["relationship_id"] == 50
    assert explanation["inference_method"] == "declared_fk"
    assert explanation["relationship_status"] == "AUTO"
    assert explanation["confidence"] == 100
    assert explanation["confidence_tier"] == "VERY_HIGH"
    assert "declares a foreign key" in explanation["why"]
    assert "trusted automatically" in explanation["recommended_action"]
    assert explanation["evidence"]
    assert explanation["weaknesses"] == []


def test_explain_inferred_relationship(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    explanation = explain_relationship(1, "u1", rel_id)
    assert explanation["inference_method"] == "name_match"
    assert explanation["relationship_status"] == "PENDING"
    assert "Suggested because" in explanation["why"]
    assert explanation["evidence"]
    assert explanation["recommended_action"]

    # Unknown id / wrong user
    assert explain_relationship(1, "u1", 999999) is None
    assert explain_relationship(1, "someone-else", rel_id) is None


# ---------------------------------------------------------------------------
# 10. Governance profile for relationship.suggestion
# ---------------------------------------------------------------------------

def test_governance_profile_for_relationship(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    profile = get_governance_profile(
        object_type=GovernedObjectType.RELATIONSHIP_SUGGESTION, relationship_id=rel_id
    )
    assert profile is not None
    assert profile.approval_state == GovernanceState.SUGGESTED
    assert profile.review_required is True
    assert profile.confidence_score is not None
    assert profile.object_type_id == GovernedObjectType.RELATIONSHIP_SUGGESTION

    # Declared FK -> AUTO_APPROVED, no review needed
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    fk_profile = get_governance_profile(
        object_type=GovernedObjectType.RELATIONSHIP_SUGGESTION, relationship_id=50
    )
    assert fk_profile.approval_state == GovernanceState.AUTO_APPROVED
    assert fk_profile.review_required is False
    assert fk_profile.can_ai_use is True

    # Unknown id
    assert get_governance_profile(
        object_type=GovernedObjectType.RELATIONSHIP_SUGGESTION, relationship_id=999999
    ) is None


# ---------------------------------------------------------------------------
# 11 & 12. approve_relationship / reject_relationship
# ---------------------------------------------------------------------------

def test_approve_relationship_pending_to_approved(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    assert approve_relationship(rel_id, "someone-else") is None  # ownership denied
    assert _row(db, rel_id)["relationship_status"] == "PENDING"  # untouched

    updated = approve_relationship(rel_id, "u1")
    assert updated["relationship_status"] == "APPROVED"
    assert updated["approved_by"] == "u1"
    assert updated["approved_at"] is not None

    try:
        approve_relationship(rel_id, "u1")
        assert False, "expected ValueError on double-approve"
    except ValueError:
        pass


def test_reject_relationship_pending_to_rejected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    updated = reject_relationship(rel_id, "u1")
    assert updated["relationship_status"] == "REJECTED"
    assert updated["approved_by"] == "u1"

    try:
        reject_relationship(rel_id, "u1")
        assert False, "expected ValueError on double-reject"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 13. Bulk approve is hard-blocked for relationship.suggestion — inferred
#     relationships may only be approved one at a time via approve_relationship()
#     (M-3 non-negotiable rule: "do not auto-approve inferred relationships").
#     Bulk reject/suppression of junk candidates remains allowed.
# ---------------------------------------------------------------------------

def test_bulk_approve_relationship_suggestion_is_hard_blocked(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    f = BulkFilter(object_type="relationship.suggestion", source_id=1)
    result = bulk_approve(f, actor_id="u1")

    assert result.affected_count == 0
    assert result.blocked_count == 1
    assert result.blocked_items[0]["blocking_policy"] == \
        "HARD_RELATIONSHIP_SUGGESTION_NO_BULK_APPROVE"
    # Candidate stays PENDING — never auto-trusted via a bulk path.
    assert _row(db, rel_id)["relationship_status"] == "PENDING"
    # Declared FK row must be completely untouched by the bulk op
    fk_row = _row(db, 50)
    assert fk_row["relationship_status"] == "AUTO"
    assert fk_row["approved_by"] is None


def test_bulk_reject_relationship_suggestion_still_allowed(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    f = BulkFilter(object_type="relationship.suggestion", source_id=1)
    result = bulk_reject(f, actor_id="u1")

    assert result.affected_count == 1
    assert _row(db, rel_id)["relationship_status"] == "REJECTED"


def test_individual_approve_relationship_still_works_after_bulk_block(tmp_path, monkeypatch):
    """The hard bulk-approve block must not affect the real, per-item governed
    approval path — that remains the intended way to trust a candidate."""
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    approve_relationship(rel_id, "u1")

    assert _row(db, rel_id)["relationship_status"] == "APPROVED"


# ---------------------------------------------------------------------------
# 14-17. Trust filter — PENDING/REJECTED excluded, APPROVED/AUTO included,
# across the active semantic, knowledge-graph, and business-context consumers.
# ---------------------------------------------------------------------------

def _visibility_snapshot(db):
    paths = discover_join_paths(1, "u1", "dbo.orders", "dbo.customers")
    semantic_visible = paths["total_paths_found"] > 0

    kg = get_related_tables(1, "u1", "dbo.customers")
    kg_tables = {r["table_fqn"] for r in kg["related_tables"]}

    biz = get_table_business_context(1, "u1", "dbo.customers")
    biz_inbound = {r["from_table_fqn"] for r in biz["relationships"]["inbound"]}

    return semantic_visible, kg_tables, biz_inbound


def test_pending_excluded_from_all_consumers(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")  # creates a PENDING orders->customers row

    semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    # PENDING inferred relationship invisible everywhere (requirement 14)
    assert semantic_visible is False
    assert "dbo.orders" not in kg_tables
    assert "dbo.orders" not in biz_inbound


def test_approved_included_in_all_consumers(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)
    approve_relationship(rel_id, "u1")

    semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    assert semantic_visible is True
    assert "dbo.orders" in kg_tables
    assert "dbo.orders" in biz_inbound


def test_rejected_excluded_from_all_consumers(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)
    reject_relationship(rel_id, "u1")

    semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    assert semantic_visible is False
    assert "dbo.orders" not in kg_tables
    assert "dbo.orders" not in biz_inbound


# ---------------------------------------------------------------------------
# Phase 2F.3 — Relationship test isolation and pre-migration behavioral
# baseline for discover_relationship_candidates(), ahead of its future
# migration onto the connection-aware shared resolver
# (data.profiling_snapshot_resolver.get_latest_profiling_snapshot). None of
# this changes data/relationship_service.py or the local
# _latest_profiling_snap_id() helper it still uses today.
# ---------------------------------------------------------------------------

# The exact, complete discover_relationship_candidates() result key set —
# confirmed directly from its return statements (data/relationship_service.py),
# not inferred from docstrings or earlier phase wording. The same dict object
# is built once and mutated in place across every return path.
_DISCOVER_RESULT_KEYS = {
    "schema_snapshot_id",
    "profiling_snapshot_id",
    "candidates_evaluated",
    "candidates_persisted",
    "candidates_discarded_low_confidence",
    "candidates_skipped_existing",
}


class _RelationshipInsertTrackingConn:
    """Wraps a real sqlite3.Connection and records each execution of
    discover_relationship_candidates()'s candidate-insert statement, without
    altering behavior. That statement isn't a separately patchable function,
    so this is the narrowest available seam for proving insert timing
    (used by the rollback and read-before-write tests below).

    `events`, if given, is a shared list other collaborators (e.g. a patched
    read function) also append to, letting a test assert relative order.
    """
    _INSERT_MARKER = "INSERT OR IGNORE INTO table_relationships"

    def __init__(self, conn: sqlite3.Connection, events: list[str] | None = None) -> None:
        self._conn = conn
        self.insert_count = 0
        self._events = events

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and self._INSERT_MARKER in sql:
            self.insert_count += 1
            if self._events is not None:
                self._events.append("insert")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# --- 1. Response shape ---

def test_response_shape_keys_and_types(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    result = discover_relationship_candidates(1, "u1")

    assert set(result.keys()) == _DISCOVER_RESULT_KEYS
    assert isinstance(result["schema_snapshot_id"], int)
    assert isinstance(result["profiling_snapshot_id"], int)
    assert isinstance(result["candidates_evaluated"], int)
    assert isinstance(result["candidates_persisted"], int)
    assert isinstance(result["candidates_discarded_low_confidence"], int)
    assert isinstance(result["candidates_skipped_existing"], int)


# --- 2. Older COMPLETE beats newer CANCELLED ---

def test_snapshot_complete_beats_newer_cancelled(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_profiling_snapshot(db, 2, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="COMPLETE")
    _insert_profiling_snapshot(db, 3, source_id=1, schema_snapshot_id=1, snapshot_version=3, status="CANCELLED")

    # Older COMPLETE snapshot's columns -> customers/orders pair (must be used).
    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=2)
    _insert_column(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.05, profiling_snapshot_id=2)
    # Newer CANCELLED snapshot's columns -> a distinguishable vendors/orders
    # pair (must NOT be used — a newer non-COMPLETE snapshot must never
    # shadow older, usable COMPLETE evidence).
    _insert_column(db, 3, "dbo.vendors", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=3)
    _insert_column(db, 4, "dbo.orders", "vendor_id", uniqueness_score=0.05, profiling_snapshot_id=3)

    result = discover_relationship_candidates(1, "u1")

    assert result["profiling_snapshot_id"] == 2
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    involved = {row["from_table_fqn"], row["to_table_fqn"]}
    assert involved == {"dbo.customers", "dbo.orders"}
    assert "dbo.vendors" not in involved


# --- 3. Older COMPLETE beats newer RUNNING ---

def test_snapshot_complete_beats_newer_running(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_profiling_snapshot(db, 2, source_id=1, schema_snapshot_id=1, snapshot_version=2, status="COMPLETE")
    _insert_profiling_snapshot(db, 3, source_id=1, schema_snapshot_id=1, snapshot_version=3, status="RUNNING")

    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=2)
    _insert_column(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.05, profiling_snapshot_id=2)
    _insert_column(db, 3, "dbo.vendors", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=3)
    _insert_column(db, 4, "dbo.orders", "vendor_id", uniqueness_score=0.05, profiling_snapshot_id=3)

    result = discover_relationship_candidates(1, "u1")

    assert result["profiling_snapshot_id"] == 2
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    involved = {row["from_table_fqn"], row["to_table_fqn"]}
    assert involved == {"dbo.customers", "dbo.orders"}
    assert "dbo.vendors" not in involved


# --- 4. No-snapshot behavior ---

def test_no_profiling_snapshot_returns_defined_result_without_insert_or_exception(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    conn = _conn(db)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','NoProfiling','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (2,2,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    # No profiling_snapshots row seeded for source_id=2 at all.

    result = discover_relationship_candidates(2, "u1")

    assert result == {
        "schema_snapshot_id": 2,
        "profiling_snapshot_id": None,
        "candidates_evaluated": 0,
        "candidates_persisted": 0,
        "candidates_discarded_low_confidence": 0,
        "candidates_skipped_existing": 0,
    }

    rels = get_relationships_for_source(2, "u1")
    assert rels == []

    conn = _conn(db)
    audit_rows = conn.execute(
        "SELECT status, user_id FROM audit_logs WHERE task_type = 'relationship_candidate_discovery'"
    ).fetchall()
    conn.close()
    assert len(audit_rows) == 1
    assert audit_rows[0]["status"] == "success"
    assert audit_rows[0]["user_id"] == "u1"


# --- 5. Source scoping ---

def test_snapshot_source_scoping(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)  # source_id=1, profiling_snapshot_id=1 (seeded by env())

    conn = _conn(db)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Other','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (2,2,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    # Other source's profiling snapshot is far newer in absolute version terms,
    # to prove it can never leak into source 1's resolution.
    _insert_profiling_snapshot(db, 99, source_id=2, schema_snapshot_id=2, snapshot_version=99, status="COMPLETE")
    _insert_column(db, 10, "dbo.vendors", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=99, source_id=2)
    _insert_column(db, 11, "dbo.orders", "vendor_id", uniqueness_score=0.05,
                    profiling_snapshot_id=99, source_id=2)

    result = discover_relationship_candidates(1, "u1")

    assert result["profiling_snapshot_id"] == 1
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    involved = {row["from_table_fqn"], row["to_table_fqn"]}
    assert involved == {"dbo.customers", "dbo.orders"}
    assert "dbo.vendors" not in involved


# --- Phase 2F.4 migration proof: None path uses the shared resolver ---

def test_none_override_invokes_resolver_once_with_source_id_and_caller_conn(tmp_path, monkeypatch):
    """Direct Phase 2F.4 migration proof: when profiling_snap_id is omitted,
    discover_relationship_candidates() must call the shared resolver exactly
    once, with source_id and the exact connection object it itself opened
    (not a new one). The old local helper is gone as of Phase 2F.5 (see the
    structural tests below), so there's nothing left to patch a
    forbidden-call guard onto here."""
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    opened_connections = []

    def _tracking_get_connection():
        c = _db_conn(db)
        opened_connections.append(c)
        return c

    monkeypatch.setattr("data.relationship_service.get_connection", _tracking_get_connection)

    resolver_calls = []
    real_resolver = get_latest_profiling_snapshot

    def _tracking_resolver(source_id, conn=None):
        resolver_calls.append((source_id, conn))
        return real_resolver(source_id, conn=conn)

    monkeypatch.setattr("data.relationship_service.get_latest_profiling_snapshot", _tracking_resolver)

    result = discover_relationship_candidates(1, "u1")

    assert result["candidates_persisted"] == 1
    assert len(opened_connections) == 1
    assert len(resolver_calls) == 1
    called_source_id, called_conn = resolver_calls[0]
    assert called_source_id == 1
    assert called_conn is opened_connections[0]  # exact caller-owned conn, not a new one


# --- 6. Explicit profiling_snap_id override ---

def test_explicit_profiling_snapshot_override_positive_bypasses_latest_resolution(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _insert_profiling_snapshot(db, 5, source_id=1, schema_snapshot_id=1, snapshot_version=5, status="COMPLETE")
    _insert_profiling_snapshot(db, 6, source_id=1, schema_snapshot_id=1, snapshot_version=6, status="COMPLETE")

    # Explicitly-targeted (older) snapshot's columns -> customers/orders pair.
    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=5)
    _insert_column(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.05, profiling_snapshot_id=5)
    # Newer snapshot's columns -> a distinguishable pair that must NOT be used.
    _insert_column(db, 3, "dbo.vendors", "id", is_primary_key=1, is_identity=1,
                    uniqueness_score=1.0, profiling_snapshot_id=6)
    _insert_column(db, 4, "dbo.orders", "vendor_id", uniqueness_score=0.05, profiling_snapshot_id=6)

    # Scoped to this test only. Raising proves the override short-circuits
    # the snapshot-resolution fallback entirely — the resolver must never be
    # consulted when an explicit positive profiling_snap_id is supplied. (The
    # old local helper this used to also guard against was retired in Phase
    # 2F.5 — see the structural tests below; there's nothing left to patch.)
    def _resolver_must_not_be_called(source_id, conn=None):
        raise AssertionError("get_latest_profiling_snapshot must not be called when profiling_snap_id is supplied")

    monkeypatch.setattr("data.relationship_service.get_latest_profiling_snapshot", _resolver_must_not_be_called)

    result = discover_relationship_candidates(1, "u1", profiling_snap_id=5)

    assert result["profiling_snapshot_id"] == 5
    assert result["candidates_persisted"] == 1

    rel_id = _pending_id(db)
    row = _row(db, rel_id)
    involved = {row["from_table_fqn"], row["to_table_fqn"]}
    assert involved == {"dbo.customers", "dbo.orders"}
    assert "dbo.vendors" not in involved


def test_explicit_profiling_snapshot_override_zero_falls_back_to_latest(tmp_path, monkeypatch):
    """Documents current production behavior exactly as-is: `0` is falsy in
    `if profiling_snap_id: ... else: ...`, so profiling_snap_id=0 is NOT
    treated as an override — it falls back to latest-snapshot resolution via
    the shared resolver, identically to omitting the parameter. Phase 2F.4
    changes the resolution mechanism (shared resolver instead of the local
    helper) but not this semantic."""
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)  # profiling_snapshot_id=1 (seeded by env())

    resolver_calls = []
    real_resolver = get_latest_profiling_snapshot

    def _tracking_resolver(source_id, conn=None):
        resolver_calls.append(source_id)
        return real_resolver(source_id, conn=conn)

    monkeypatch.setattr("data.relationship_service.get_latest_profiling_snapshot", _tracking_resolver)

    result = discover_relationship_candidates(1, "u1", profiling_snap_id=0)

    assert result["profiling_snapshot_id"] == 1  # resolved via fallback, not literal 0
    assert result["candidates_persisted"] == 1
    assert resolver_calls == [1]  # resolver was called (fallback path), not skipped


# --- 7. Connection count ---

def test_connection_count_is_one(tmp_path, monkeypatch):
    """Phase 2F.4: discover_relationship_candidates() now calls the shared
    resolver via get_latest_profiling_snapshot(source_id, conn=conn) — since
    conn is supplied, the resolver never calls its own get_connection(), so
    the Relationship-owned connection count stays 1 and the resolver-owned
    count stays 0."""
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    rel_calls = {"n": 0}
    resolver_calls = {"n": 0}

    def _counting_rel_get_connection():
        rel_calls["n"] += 1
        return _db_conn(db)

    def _counting_resolver_get_connection():
        resolver_calls["n"] += 1
        return _db_conn(db)

    monkeypatch.setattr("data.relationship_service.get_connection", _counting_rel_get_connection)
    monkeypatch.setattr("data.profiling_snapshot_resolver.get_connection", _counting_resolver_get_connection)

    # Explicit reset: env() and the seeding above run before these counting
    # wrappers exist, so they cannot have been counted regardless — this
    # makes that guarantee explicit rather than relying on call ordering.
    rel_calls["n"] = 0
    resolver_calls["n"] = 0

    result = discover_relationship_candidates(1, "u1")

    assert result["candidates_persisted"] == 1
    assert rel_calls["n"] == 1
    assert resolver_calls["n"] == 0
    assert rel_calls["n"] + resolver_calls["n"] == 1


# --- 8. Rollback after partial failure ---

def test_rollback_after_partial_failure_leaves_no_partial_rows(tmp_path, monkeypatch):
    """Forces a deterministic exception while processing the second of two
    candidates, after the first candidate's INSERT OR IGNORE has already
    executed but strictly before conn.commit(). The two candidates are both
    Pass-1 (within-bucket, name-matched) pairs — the only pass whose order is
    guaranteed, since Pass 2 (domain/entity cross-bucket pairs) iterates a
    set and is not order-stable. Patch point is _value_overlap_score, called
    exactly once per candidate immediately before scoring/insert — the
    narrowest available per-candidate seam.

    Since discover_relationship_candidates() has no except clause around this
    loop, the exception is never swallowed by _audit_discovery() (which only
    runs after conn.commit(), never reached here) and propagates straight to
    the outer `finally: conn.close()`, which rolls back the uncommitted
    transaction — proving persistence is all-or-nothing per run, not
    per-candidate.
    """
    db = env(tmp_path, monkeypatch)
    _insert_column(db, 1, "dbo.customers", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)
    _insert_column(db, 2, "dbo.orders", "customer_id", uniqueness_score=0.05)
    _insert_column(db, 3, "dbo.orders", "product_id", uniqueness_score=0.05)
    _insert_column(db, 4, "dbo.products", "id", is_primary_key=1, is_identity=1, uniqueness_score=1.0)

    tracked_conn = _RelationshipInsertTrackingConn(_db_conn(db))
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: tracked_conn)

    call_count = {"n": 0}
    insert_count_at_failure = {}

    def _overlap_second_call_raises(conn, from_col_id, to_col_id):
        call_count["n"] += 1
        if call_count["n"] == 2:
            insert_count_at_failure["value"] = tracked_conn.insert_count
            raise RuntimeError("forced-failure-for-rollback-test")
        return None

    monkeypatch.setattr("data.relationship_service._value_overlap_score", _overlap_second_call_raises)

    with pytest.raises(RuntimeError, match="forced-failure-for-rollback-test"):
        discover_relationship_candidates(1, "u1")

    # Proves the first candidate's INSERT OR IGNORE actually executed before
    # the forced failure — without this, a failure before any insert would
    # not demonstrate partial-write rollback at all.
    assert insert_count_at_failure["value"] == 1
    assert tracked_conn.insert_count == 1  # no insert for the second (failed) candidate

    conn = _conn(db)
    remaining = conn.execute("SELECT COUNT(*) AS n FROM table_relationships").fetchone()["n"]
    audit_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_logs WHERE task_type = 'relationship_candidate_discovery'"
    ).fetchone()["n"]
    conn.close()
    assert remaining == 0    # the uncommitted first insert was rolled back on close
    assert audit_rows == 0   # _audit_discovery() runs after commit(); never reached


# --- 9. Read-only phase before inserts ---

def test_snapshot_and_column_reads_precede_first_insert(tmp_path, monkeypatch):
    """Narrow ordering evidence only (not a full SQL-sequence assertion):
    the profiling-column read (get_key_candidate_columns, which depends on
    snapshot resolution having already happened) is observed to occur before
    the first table_relationships insert.
    """
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)

    events: list[str] = []
    tracked_conn = _RelationshipInsertTrackingConn(_db_conn(db), events=events)
    monkeypatch.setattr("data.relationship_service.get_connection", lambda: tracked_conn)

    real_get_columns = discover_relationship_candidates.__globals__["get_key_candidate_columns"]

    def _tracking_get_columns(conn, source_id, profiling_snapshot_id):
        events.append("read_columns")
        return real_get_columns(conn, source_id, profiling_snapshot_id)

    monkeypatch.setattr("data.relationship_service.get_key_candidate_columns", _tracking_get_columns)

    result = discover_relationship_candidates(1, "u1")

    assert result["candidates_persisted"] == 1
    assert "read_columns" in events
    assert "insert" in events
    assert events.index("read_columns") < events.index("insert")


# --- Resolver fixture isolation proof ---

def test_resolver_fixture_patches_temp_database_not_default(tmp_path, monkeypatch):
    """Proves data.profiling_snapshot_resolver.get_connection is now
    redirected to the same temp DB as the rest of this suite: seeds a
    uniquely identifiable snapshot (id/version 4242, which cannot exist in
    any other database) directly in the temp DB, then resolves it through
    the real resolver function. No assertion depends on the contents or
    absence of data/toolsmith.db."""
    db = env(tmp_path, monkeypatch)
    _insert_profiling_snapshot(db, 4242, source_id=1, schema_snapshot_id=1, snapshot_version=4242, status="COMPLETE")

    result = get_latest_profiling_snapshot(1)

    assert result is not None
    assert result.id == 4242
    assert result.snapshot_version == 4242


# ---------------------------------------------------------------------------
# Phase 2F.5 — structural proof that the legacy local snapshot helper
# (_latest_profiling_snap_id) is gone from relationship_service.py and that
# discover_relationship_candidates() uses the shared resolver directly.
# ---------------------------------------------------------------------------

def test_relationship_service_has_no_legacy_snapshot_helper():
    import data.relationship_service as relationship_service_module

    assert not hasattr(relationship_service_module, "_latest_profiling_snap_id")


def test_relationship_service_source_has_no_legacy_helper_reference():
    import data.relationship_service as relationship_service_module

    source = inspect.getsource(relationship_service_module)
    assert "_latest_profiling_snap_id" not in source


def test_discover_relationship_candidates_uses_shared_resolver():
    """Narrow AST check scoped to discover_relationship_candidates()'s own
    source only: exactly one get_latest_profiling_snapshot(...) call, with
    source_id passed positionally and conn=conn passed by keyword."""
    source = inspect.getsource(discover_relationship_candidates)
    tree = ast.parse(source)

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


def test_repository_has_no_live_production_legacy_snapshot_helpers():
    """AST-based (not substring) search of every top-level module in data/
    for a live def _latest_profiling_snap_id(...). Scoped to production
    Python files only, so the historical comments/docstrings in
    tests/test_phase{3,4,5,6,8}_*.py describing other modules' own past
    retirements of this same pattern cannot cause a false failure here."""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    violations = []
    for fname in os.listdir(data_dir):
        if not fname.endswith(".py"):
            continue
        path = os.path.join(data_dir, fname)
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_latest_profiling_snap_id":
                violations.append(fname)

    assert violations == []
