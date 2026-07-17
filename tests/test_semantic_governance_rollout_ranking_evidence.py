"""
Tests for Milestone M-23 (Phase 6.5) — ranking evidence demonstration.

Proves the milestone's central claim without touching the ranking algorithm
itself: maturing a table's dictionary approval through the reused governed-
automation path (run_semantic_governance_rollout -> run_dictionary_curation)
changes the evidence data.query_planning_service._score_table_authority()
already computes from — a real "Dictionary Approved" bonus appears where it
didn't before. It also documents, as an assertion (not just prose), that
domain/entity assignment *governance* maturity alone does NOT move this
bonus — that signal already fires on any non-Unknown domain/entity value
regardless of approval state (data/business_knowledge_service.py's
governance_section: "domain_assigned": domain != "Unknown", no assignment_source
check) — so this milestone's ranking improvement specifically comes from the
dictionary-approval side, not the domain/entity governance work.

Run from the project root:
    venv/Scripts/pytest tests/test_semantic_governance_rollout_ranking_evidence.py -v
"""
from __future__ import annotations

import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-ranking-evidence-secret-long-enough-123")
os.environ.setdefault("USER_ID_SALT", "test-ranking-evidence-salt-long-enough-123")

import data.models as models
from data.semantic_governance_rollout_service import run_semantic_governance_rollout

_NOW = "2026-07-13T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.semantic_governance_rollout_service",
    "data.dictionary_curation_service",
    "data.governance_service",
    "data.business_knowledge_service",
    "data.review_segmentation_service",
    "data.query_planning_service",
    "data.dictionary_service",
    "data.knowledge_graph_service",
    "data.domain_service",
    "data.entity_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "ranking.db")
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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',1,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    table_fqn = "dbo.adf_placements"
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, classification_confidence, created_at, updated_at) "
        "VALUES (1,1,1,?,?,?, 'Master','COMPLETE',50000,0.9,?,?)",
        (table_fqn, "adf_placements", "dbo", _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?,0,?,?,?)",
        (table_fqn, "adf_placements", "dbo", "TABLE", "Placements", "rule_based", _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, uniqueness_score, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (1,1,?,?,'INTEGER',1,1.0,0,0,?,?)",
        (table_fqn, "id", _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_approved, pii_risk, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,0,0,?,?,?)",
        (table_fqn, "id", "id", "rule_based", _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO domain_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, assignment_source, "
        " created_at, updated_at) VALUES (1,1,?,?,?,?,?,?)",
        (table_fqn, "Student Lifecycle", 0.97, "rule", _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, assignment_source, "
        " created_at, updated_at) VALUES (1,1,?,?,?,?,?,?)",
        (table_fqn, "Student", 0.97, "rule", _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path, table_fqn


def _authority(table_fqn):
    from data.business_knowledge_service import get_table_business_context
    from data.query_planning_service import _score_table_authority
    ctx = get_table_business_context(1, "u1", table_fqn)
    return _score_table_authority(table_fqn, ctx)


def test_dictionary_maturation_adds_authority_evidence(tmp_path, monkeypatch):
    db, table_fqn = env(tmp_path, monkeypatch)

    before = _authority(table_fqn)
    assert "Dictionary Approved" not in before["reasons"]

    run_semantic_governance_rollout(1, "u1", dry_run=False, actor_id="system:test-ranking")

    after = _authority(table_fqn)
    assert "Dictionary Approved" in after["reasons"]
    assert after["bonus"] > before["bonus"]


def test_domain_entity_governance_maturity_alone_does_not_move_authority_bonus(tmp_path, monkeypatch):
    """
    Documents (as a running assertion, not just prose) that domain/entity
    assignment governance maturity is orthogonal to _score_table_authority's
    bonus: "Domain = ..."/"Entity = ..." reasons and their +0.05/+0.07 bonus
    already fire on any non-Unknown value (business_knowledge_service's
    governance_section computes domain_assigned/entity_assigned from the
    value alone), independent of assignment_source. Maturing rule -> auto_governance
    changes trust/audit state, not ranking evidence.
    """
    db, table_fqn = env(tmp_path, monkeypatch)

    before = _authority(table_fqn)
    assert "Domain = Student Lifecycle" in before["reasons"]
    assert "Entity = Student" in before["reasons"]
    domain_bonus_before = before["bonus"]

    from data.domain_service import auto_mature_domain_assignment
    from data.entity_service import auto_mature_entity_assignment
    auto_mature_domain_assignment(1, table_fqn, actor_id="system:test-ranking")
    auto_mature_entity_assignment(1, table_fqn, actor_id="system:test-ranking")

    after = _authority(table_fqn)
    assert "Domain = Student Lifecycle" in after["reasons"]
    assert "Entity = Student" in after["reasons"]
    # Same reasons, same bonus — governance state change alone moved nothing here.
    assert after["bonus"] == domain_bonus_before
