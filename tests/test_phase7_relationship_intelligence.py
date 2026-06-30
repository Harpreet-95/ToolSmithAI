"""
Tests for Program 3 Phase 1 — Relationship Intelligence & Join Discovery Engine.

Uses the real production schema (data.models.init_db) against a per-test
temp SQLite file, rather than a hand-rolled minimal schema, so these tests
can never drift out of sync with the actual table_relationships columns the
way the older phase3-6 fixtures did (see Step 6 fixture fix).

Run from the project root:
    venv/Scripts/pytest tests/test_phase7_relationship_intelligence.py -v
"""
import os
import sqlite3
from datetime import datetime, timezone

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
    get_governance_profile,
)
from data.lineage_service import get_upstream_lineage
from data.semantic_layer_service import discover_join_paths
from data.knowledge_graph_service import get_related_tables
from data.business_knowledge_service import get_table_business_context


# ---------------------------------------------------------------------------
# DB setup — full production schema via models.init_db(), patched onto a
# per-test temp file and wired into every consumer module's get_connection.
# ---------------------------------------------------------------------------

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.relationship_service",
    "data.governance_service",
    "data.lineage_service",
    "data.semantic_layer_service",
    "data.knowledge_graph_service",
    "data.business_knowledge_service",
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
):
    conn = _conn(db_path)
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, is_identity, uniqueness_score, guid_match_rate, semantic_type, "
        " created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,?,?,?,?,?,?)",
        (col_id, table_fqn, column_name, data_type, is_primary_key, is_identity,
         uniqueness_score, guid_match_rate, semantic_type, _NOW, _NOW),
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
# 13. Bulk approve works for relationship.suggestion (no second workflow)
# ---------------------------------------------------------------------------

def test_bulk_approve_relationship_suggestion(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")
    rel_id = _pending_id(db)

    f = BulkFilter(object_type="relationship.suggestion", source_id=1)
    result = bulk_approve(f, actor_id="u1")

    assert result.affected_count == 1
    assert _row(db, rel_id)["relationship_status"] == "APPROVED"
    # Declared FK row must be completely untouched by the bulk op
    fk_row = _row(db, 50)
    assert fk_row["relationship_status"] == "AUTO"
    assert fk_row["approved_by"] is None


# ---------------------------------------------------------------------------
# 14-17. Trust filter — PENDING/REJECTED excluded, APPROVED/AUTO included,
# across lineage, semantic, knowledge graph, and business context.
# ---------------------------------------------------------------------------

def _visibility_snapshot(db):
    lineage = get_upstream_lineage(1, "u1", "dbo.orders")
    lineage_tables = {n["table_fqn"] for n in lineage["upstream"]}

    paths = discover_join_paths(1, "u1", "dbo.orders", "dbo.customers")
    semantic_visible = paths["total_paths_found"] > 0

    kg = get_related_tables(1, "u1", "dbo.customers")
    kg_tables = {r["table_fqn"] for r in kg["related_tables"]}

    biz = get_table_business_context(1, "u1", "dbo.customers")
    biz_inbound = {r["from_table_fqn"] for r in biz["relationships"]["inbound"]}

    return lineage_tables, semantic_visible, kg_tables, biz_inbound


def test_pending_excluded_from_all_consumers(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _basic_name_match_pair(db)
    _insert_declared_fk(db, 50, "dbo.orders", "product_id", "dbo.products", "id")
    discover_relationship_candidates(1, "u1")  # creates a PENDING orders->customers row

    lineage_tables, semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    # AUTO foreign key still visible (requirement 17)
    assert "dbo.products" in lineage_tables
    # PENDING inferred relationship invisible everywhere (requirement 14)
    assert "dbo.customers" not in lineage_tables
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

    lineage_tables, semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    assert "dbo.products" in lineage_tables       # AUTO still visible
    assert "dbo.customers" in lineage_tables      # requirement 15
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

    lineage_tables, semantic_visible, kg_tables, biz_inbound = _visibility_snapshot(db)

    assert "dbo.products" in lineage_tables       # AUTO still visible (requirement 17)
    assert "dbo.customers" not in lineage_tables  # requirement 16
    assert semantic_visible is False
    assert "dbo.orders" not in kg_tables
    assert "dbo.orders" not in biz_inbound
