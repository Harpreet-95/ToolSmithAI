"""
Tests for Milestone M-23 (Phase 6.5) — Enterprise Semantic Governance Rollout.

Built on the real production schema (data.models.init_db, which seeds the new
POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_ASSIGNMENTS / POLICY_REQUIRE_HUMAN_ASSIGNMENTS
rows) against a per-test temp SQLite file, following the same pattern as
tests/test_dictionary_curation_service.py.

Covers every regression category the milestone brief lists: automatic
governance progression, policy enforcement, audit generation, rollback
safety, source isolation, deterministic approvals, blocked ambiguous assets,
blocked sensitive assets, dry-run-is-read-only, and reuse of
run_dictionary_curation (not reimplementation).

Run from the project root:
    venv/Scripts/pytest tests/test_semantic_governance_rollout_service.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-governance-rollout-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-governance-rollout-salt-long-enough-1")

import data.models as models
from data.semantic_governance_rollout_service import (
    classify_asset_maturity,
    run_semantic_governance_rollout,
    MATURITY_TRUSTED,
    MATURITY_REVIEW_REQUIRED,
    MATURITY_BLOCKED,
    MATURITY_UNKNOWN,
)

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
    db_path = str(tmp_path / "rollout.db")
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
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Other Source','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
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
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    # Second source, its own schema/profiling snapshot — used by the
    # source-isolation test (a rollout run for source 1 must never touch
    # source 2's rows, which requires their own profiling_snapshot_id since
    # profiling_column_profiles is unique on (snapshot_id, table_fqn, column)).
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (2,2,1,'mssql',2,'{}',?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, created_at) "
        "VALUES (2,2,2,1,?)", (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, table_class="Master", row_count=50000, approved=False,
                table_type="TABLE", source_id=1):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash((source_id, table_fqn))) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, classification_confidence, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,'COMPLETE',?,0.9,?,?)",
        (tid, source_id, source_id, table_fqn, name, schema, table_class, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (?,1,?,?,?,?,?,?,?,?,?)",
        (source_id, table_fqn, name, schema, table_type, name.capitalize(), int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_column(db, table_fqn, col_name, *, pii=0, source_id=1):
    c = _c(db)
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, uniqueness_score, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?,?,?,?,'INTEGER',1,1.0,?,0,?,?)",
        (source_id, source_id, table_fqn, col_name, pii, _NOW, _NOW),
    )
    c.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_approved, pii_risk, generation_method, created_at, updated_at) "
        "VALUES (?,1,?,?,?,0,?,?,?,?)",
        (source_id, table_fqn, col_name, col_name, pii, "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_domain(db, table_fqn, domain, *, confidence=0.95, assignment_source="rule", source_id=1):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO domain_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, assignment_source, "
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (source_id, source_id, table_fqn, domain, confidence, assignment_source, _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_entity(db, table_fqn, entity, *, confidence=0.95, assignment_source="rule", source_id=1):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, assignment_source, "
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (source_id, source_id, table_fqn, entity, confidence, assignment_source, _NOW, _NOW),
    )
    c.commit()
    c.close()


def _domain_row(db, table_fqn, source_id=1):
    c = _c(db)
    row = c.execute(
        "SELECT * FROM domain_assignments WHERE source_id = ? AND table_fqn = ?",
        (source_id, table_fqn),
    ).fetchone()
    c.close()
    return dict(row) if row else None


def _governance_events(db, object_type_id, object_id=None):
    c = _c(db)
    if object_id:
        rows = c.execute(
            "SELECT * FROM governance_approval_events WHERE object_type_id = ? AND object_id = ?",
            (object_type_id, object_id),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM governance_approval_events WHERE object_type_id = ?", (object_type_id,)
        ).fetchall()
    c.close()
    return [dict(r) for r in rows]


def _well_governed_table(db, table_fqn="dbo.adf_placements", *, domain_confidence=0.97,
                          entity_confidence=0.97, source_id=1):
    _add_table(db, table_fqn, table_class="Master", row_count=50000, approved=False, source_id=source_id)
    _add_column(db, table_fqn, "id", source_id=source_id)
    _add_domain(db, table_fqn, "Student Lifecycle", confidence=domain_confidence, source_id=source_id)
    _add_entity(db, table_fqn, "Student", confidence=entity_confidence, source_id=source_id)


# ---------------------------------------------------------------------------
# classify_asset_maturity
# ---------------------------------------------------------------------------

def test_maturity_trusted_after_human_lock(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", assignment_source="human")

    result = classify_asset_maturity(1, "u1", object_type="domain.assignment", table_fqn="dbo.adf_placements")
    assert result["status"] == MATURITY_TRUSTED


def test_maturity_trusted_after_auto_governance(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", assignment_source="auto_governance")

    result = classify_asset_maturity(1, "u1", object_type="domain.assignment", table_fqn="dbo.adf_placements")
    assert result["status"] == MATURITY_TRUSTED


def test_maturity_blocked_high_risk_domain(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.gl_transactions", table_class="Master", approved=False)
    _add_column(db, "dbo.gl_transactions", "id")
    _add_domain(db, "dbo.gl_transactions", "Finance", confidence=0.99)

    result = classify_asset_maturity(1, "u1", object_type="domain.assignment", table_fqn="dbo.gl_transactions")
    assert result["status"] == MATURITY_BLOCKED
    assert "high-risk" in result["reason"].lower() or "finance" in result["reason"].lower()


def test_maturity_blocked_unconfirmed_pii_column(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", approved=False)
    _add_column(db, "dbo.adf_placements", "ssn", pii=1)

    result = classify_asset_maturity(1, "u1", object_type="dict.column", table_fqn="dbo.adf_placements", column_name="ssn")
    assert result["status"] == MATURITY_BLOCKED
    assert "pii" in result["reason"].lower()


def test_maturity_review_required_ambiguous_sibling(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db, "dbo.adf_clients_a")
    _add_domain(db, "dbo.adf_clients_a", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_a", "Client", confidence=0.97)
    _well_governed_table(db, "dbo.adf_clients_b")
    _add_domain(db, "dbo.adf_clients_b", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_b", "Client", confidence=0.97)

    result = classify_asset_maturity(1, "u1", object_type="domain.assignment", table_fqn="dbo.adf_clients_a")
    assert result["status"] == MATURITY_REVIEW_REQUIRED
    assert "ambigu" in result["reason"].lower() or "margin" in result["reason"].lower()


def test_maturity_unknown_no_assignment(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orphan", approved=False)

    result = classify_asset_maturity(1, "u1", object_type="domain.assignment", table_fqn="dbo.orphan")
    assert result["status"] == MATURITY_UNKNOWN


def test_maturity_unknown_nonexistent_source(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    result = classify_asset_maturity(999, "u1", object_type="domain.assignment", table_fqn="dbo.anything")
    assert result["status"] == MATURITY_UNKNOWN


# ---------------------------------------------------------------------------
# run_semantic_governance_rollout — automatic governance progression
# ---------------------------------------------------------------------------

def test_dry_run_is_read_only(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)

    result = run_semantic_governance_rollout(1, "u1", dry_run=True)
    assert result["dry_run"] is True
    assert len(result["assignments"]["auto_approved"]) >= 2  # domain + entity

    row = _domain_row(db, "dbo.adf_placements")
    assert row["assignment_source"] == "rule"  # unchanged — dry run must not write
    assert _governance_events(db, "domain.assignment") == []


def test_non_dry_run_matures_and_writes_audit_trail(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)

    result = run_semantic_governance_rollout(1, "u1", dry_run=False, actor_id="system:test-rollout")
    matured_fqns = {e["table_fqn"] for e in result["assignments"]["auto_approved"]}
    assert "dbo.adf_placements" in matured_fqns

    row = _domain_row(db, "dbo.adf_placements")
    assert row["assignment_source"] == "auto_governance"

    events = _governance_events(db, "domain.assignment", "1:dbo.adf_placements")
    assert len(events) == 1
    assert events[0]["to_state"] == "AUTO_APPROVED"
    assert events[0]["actor_id"] == "system:test-rollout"


def test_reuses_dictionary_curation_for_dict_half(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)

    with patch(
        "data.semantic_governance_rollout_service.run_dictionary_curation",
        wraps=__import__("data.dictionary_curation_service", fromlist=["run_dictionary_curation"]).run_dictionary_curation,
    ) as spy:
        run_semantic_governance_rollout(1, "u1", dry_run=True, actor_id="system:test-rollout")
        spy.assert_called_once_with(1, "u1", dry_run=True, actor_id="system:test-rollout")


# ---------------------------------------------------------------------------
# Never auto-approved / policy enforcement
# ---------------------------------------------------------------------------

def test_high_risk_domain_never_matured(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.gl_transactions", table_class="Master", approved=False)
    _add_column(db, "dbo.gl_transactions", "id")
    _add_domain(db, "dbo.gl_transactions", "Finance", confidence=0.99)
    _add_entity(db, "dbo.gl_transactions", "Unknown", confidence=0.0)

    run_semantic_governance_rollout(1, "u1", dry_run=False)
    row = _domain_row(db, "dbo.gl_transactions")
    assert row["assignment_source"] == "rule"  # never matured
    assert _governance_events(db, "domain.assignment") == []


def test_ambiguous_sibling_blocks_maturation(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db, "dbo.adf_clients_a")
    _add_domain(db, "dbo.adf_clients_a", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_a", "Client", confidence=0.97)
    _well_governed_table(db, "dbo.adf_clients_b")
    _add_domain(db, "dbo.adf_clients_b", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_b", "Client", confidence=0.97)

    run_semantic_governance_rollout(1, "u1", dry_run=False)
    row_a = _domain_row(db, "dbo.adf_clients_a")
    row_b = _domain_row(db, "dbo.adf_clients_b")
    assert row_a["assignment_source"] == "rule"
    assert row_b["assignment_source"] == "rule"


def test_low_confidence_never_matured(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db, domain_confidence=0.4, entity_confidence=0.4)

    run_semantic_governance_rollout(1, "u1", dry_run=False)
    row = _domain_row(db, "dbo.adf_placements")
    assert row["assignment_source"] == "rule"


# ---------------------------------------------------------------------------
# Rollback safety / source isolation / determinism
# ---------------------------------------------------------------------------

def test_never_overwrites_human_lock(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)
    _add_domain(db, "dbo.adf_placements", "Custom Domain", confidence=0.4, assignment_source="human")

    run_semantic_governance_rollout(1, "u1", dry_run=False)
    row = _domain_row(db, "dbo.adf_placements")
    assert row["assignment_source"] == "human"
    assert row["domain"] == "Custom Domain"


def test_matured_row_survives_subsequent_regeneration(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)
    run_semantic_governance_rollout(1, "u1", dry_run=False)

    from data.domain_service import generate_domain_assignments
    generate_domain_assignments(1, "u1")  # simulate next scheduled scan

    row = _domain_row(db, "dbo.adf_placements")
    assert row["assignment_source"] == "auto_governance"
    assert row["domain"] == "Student Lifecycle"


def test_respects_source_ownership(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db, source_id=1)
    _well_governed_table(db, "dbo.adf_placements", source_id=2)

    run_semantic_governance_rollout(1, "u1", dry_run=False)

    row_source_1 = _domain_row(db, "dbo.adf_placements", source_id=1)
    row_source_2 = _domain_row(db, "dbo.adf_placements", source_id=2)
    assert row_source_1["assignment_source"] == "auto_governance"
    assert row_source_2["assignment_source"] == "rule"  # untouched — different source


def test_dry_run_is_deterministic(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _well_governed_table(db)

    first = run_semantic_governance_rollout(1, "u1", dry_run=True, actor_id="system:test")
    second = run_semantic_governance_rollout(1, "u1", dry_run=True, actor_id="system:test")
    assert first["assignments"] == second["assignments"]
