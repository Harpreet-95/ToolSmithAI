"""
Tests for Milestone M-5, Part 4 — Autonomous Dictionary Curation.

Built on the real production schema (data.models.init_db, which seeds the
governance_policies table including the new
POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_DICTIONARY row) against a per-test temp
SQLite file, following the same pattern as test_phase9_query_planning.py.

Run from the project root:
    venv/Scripts/pytest tests/test_dictionary_curation_service.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-dictionary-curation-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-dictionary-curation-salt-long-enough-1")

import data.models as models
from data.dictionary_curation_service import evaluate_curation_eligibility, run_dictionary_curation

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.dictionary_curation_service",
    "data.governance_service",
    "data.business_knowledge_service",
    "data.review_segmentation_service",
    "data.query_planning_service",
    "data.dictionary_service",
    "data.knowledge_graph_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "curation.db")
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
        "VALUES (1,1,1,1,?)", (_NOW,),
    )
    conn.commit()
    conn.close()
    return db_path


def _c(db_path):
    return _db_conn(db_path)


def _add_table(db, table_fqn, *, table_class="Master", row_count=50000, approved=False, table_type="TABLE"):
    name = table_fqn.split(".")[-1]
    schema = table_fqn.split(".")[0]
    c = _c(db)
    tid = abs(hash(table_fqn)) % 10000
    c.execute(
        "INSERT OR REPLACE INTO profiling_table_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, "
        " table_class, profiling_status, exact_row_count, classification_confidence, created_at, updated_at) "
        "VALUES (?,1,1,?,?,?,?,'COMPLETE',?,0.9,?,?)",
        (tid, table_fqn, name, schema, table_class, row_count, _NOW, _NOW),
    )
    c.execute(
        "INSERT OR REPLACE INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
        " business_name, is_approved, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?,?,?,?,?)",
        (table_fqn, name, schema, table_type, name.capitalize(), int(approved), "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_column(db, table_fqn, col_name, *, pii=0):
    c = _c(db)
    c.execute(
        "INSERT INTO profiling_column_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        " is_primary_key, uniqueness_score, pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (1,1,?,?,'INTEGER',1,1.0,?,0,?,?)",
        (table_fqn, col_name, pii, _NOW, _NOW),
    )
    c.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        " is_approved, pii_risk, generation_method, created_at, updated_at) "
        "VALUES (1,1,?,?,?,0,?,?,?,?)",
        (table_fqn, col_name, col_name, pii, "rule_based", _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_domain(db, table_fqn, domain, *, confidence=0.95):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO domain_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, domain, confidence, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?)",
        (table_fqn, domain, confidence, _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_entity(db, table_fqn, entity, *, confidence=0.95):
    c = _c(db)
    c.execute(
        "INSERT OR REPLACE INTO entity_assignments "
        "(source_id, profiling_snapshot_id, table_fqn, entity, confidence, created_at, updated_at) "
        "VALUES (1,1,?,?,?,?,?)",
        (table_fqn, entity, confidence, _NOW, _NOW),
    )
    c.commit()
    c.close()


def _add_fk(db, from_fqn, from_col, to_fqn, to_col):
    c = _c(db)
    fs, ft = from_fqn.split(".")
    ts, tt = to_fqn.split(".")
    c.execute(
        "INSERT INTO table_relationships "
        "(source_id, snapshot_id, from_schema, from_table, from_table_fqn, from_column, "
        " to_schema, to_table, to_table_fqn, to_column, relationship_name, relationship_type, "
        " confidence, evidence_json, created_at, relationship_status) "
        "VALUES (1,1,?,?,?,?,?,?,?,?,?,'FOREIGN_KEY',1.0,'{}',?,'AUTO')",
        (fs, ft, from_fqn, from_col, ts, tt, to_fqn, to_col, f"FK_{from_fqn}", _NOW),
    )
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# Safe auto-approval
# ---------------------------------------------------------------------------

def test_high_confidence_well_governed_table_is_eligible(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.97)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.97)
    _add_fk(db, "dbo.adf_placements", "student_id", "dbo.adf_students", "id")

    decision = evaluate_curation_eligibility(1, "u1", "dbo.adf_placements")
    assert decision["eligible"] is True
    assert decision["blocking_reasons"] == []


def test_dry_run_makes_no_writes(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.97)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.97)

    result = run_dictionary_curation(1, "u1", dry_run=True)
    assert result["dry_run"] is True
    assert len(result["auto_approved"]) >= 1

    conn = _c(db)
    row = conn.execute(
        "SELECT is_approved FROM data_dictionary_tables WHERE table_fqn = 'dbo.adf_placements'"
    ).fetchone()
    conn.close()
    assert row["is_approved"] == 0  # unchanged — dry run must not write


def test_non_dry_run_writes_auto_approved_and_audit_event(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.97)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.97)

    result = run_dictionary_curation(1, "u1", dry_run=False, actor_id="system:test-curation")
    assert any(e["table_fqn"] == "dbo.adf_placements" for e in result["auto_approved"])

    conn = _c(db)
    row = conn.execute(
        "SELECT is_approved FROM data_dictionary_tables WHERE table_fqn = 'dbo.adf_placements'"
    ).fetchone()
    assert row["is_approved"] == 1

    event = conn.execute(
        "SELECT * FROM governance_approval_events WHERE object_id = '1:dbo.adf_placements' "
        "AND to_state = 'AUTO_APPROVED'"
    ).fetchone()
    conn.close()
    assert event is not None
    assert event["actor_id"] == "system:test-curation"


# ---------------------------------------------------------------------------
# Never auto-approved
# ---------------------------------------------------------------------------

def test_pii_risk_column_blocks_auto_approval(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_column(db, "dbo.adf_placements", "ssn", pii=1)
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.97)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.97)

    decision = evaluate_curation_eligibility(1, "u1", "dbo.adf_placements")
    assert decision["eligible"] is False
    assert any("pii" in r.lower() or "review group" in r.lower() for r in decision["blocking_reasons"])


def test_temporary_table_blocked_from_auto_approval(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements_temp", table_class="Transactional", row_count=100, approved=False)
    _add_column(db, "dbo.adf_placements_temp", "id")
    _add_domain(db, "dbo.adf_placements_temp", "Student Lifecycle", confidence=0.97)
    _add_entity(db, "dbo.adf_placements_temp", "Student", confidence=0.97)

    decision = evaluate_curation_eligibility(1, "u1", "dbo.adf_placements_temp")
    assert decision["eligible"] is False
    assert any("Review group" in r for r in decision["blocking_reasons"])


def test_low_confidence_table_blocked_from_auto_approval(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.4)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.4)

    decision = evaluate_curation_eligibility(1, "u1", "dbo.adf_placements")
    assert decision["eligible"] is False


def test_ambiguous_sibling_candidate_blocks_auto_approval(tmp_path, monkeypatch):
    # Two tables sharing the same entity with comparable authority — neither
    # should be auto-approved (mirrors M-2/M-4's ambiguity-refusal behavior).
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_clients_a", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_clients_a", "id")
    _add_domain(db, "dbo.adf_clients_a", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_a", "Client", confidence=0.97)

    _add_table(db, "dbo.adf_clients_b", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_clients_b", "id")
    _add_domain(db, "dbo.adf_clients_b", "Operations", confidence=0.97)
    _add_entity(db, "dbo.adf_clients_b", "Client", confidence=0.97)

    decision = evaluate_curation_eligibility(1, "u1", "dbo.adf_clients_a")
    assert decision["eligible"] is False
    assert any("ambiguity" in r.lower() or "ambiguous" in r.lower() or "margin" in r.lower()
               for r in decision["blocking_reasons"])


def test_inferred_relationships_never_auto_approved_by_this_module():
    # dictionary_curation_service only ever calls approve_table_dictionary/
    # approve_column_dictionary — it has no code path touching
    # relationship.suggestion at all, so the existing hard-blocked bulk
    # relationship-approval policy (M-3) is untouched by construction.
    import data.dictionary_curation_service as dcs
    import inspect
    src = inspect.getsource(dcs)
    assert "approve_relationship" not in src
    assert "relationship.suggestion" not in src


# ---------------------------------------------------------------------------
# Existing callers unaffected (default kwarg)
# ---------------------------------------------------------------------------

def test_existing_approve_table_dictionary_default_still_writes_human_approved(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", approved=False)
    _add_column(db, "dbo.adf_placements", "id")

    from data.dictionary_service import approve_table_dictionary
    result = approve_table_dictionary(1, "u1", "dbo.adf_placements")
    assert result["approved"] is True

    conn = _c(db)
    event = conn.execute(
        "SELECT * FROM governance_approval_events WHERE object_id = '1:dbo.adf_placements'"
    ).fetchone()
    conn.close()
    assert event["to_state"] == "HUMAN_APPROVED"


# ---------------------------------------------------------------------------
# Seeded governance policy (data/models.py)
# ---------------------------------------------------------------------------

def test_seeded_auto_approve_dictionary_policy_exists_with_expected_priority(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    conn = _c(db)
    policy = conn.execute(
        "SELECT * FROM governance_policies WHERE policy_name = "
        "'POLICY_AUTO_APPROVE_HIGH_CONFIDENCE_DICTIONARY'"
    ).fetchone()
    catch_all = conn.execute(
        "SELECT * FROM governance_policies WHERE policy_name = 'POLICY_REQUIRE_HUMAN_DICT_ENTRIES'"
    ).fetchone()
    conn.close()

    assert policy is not None
    assert policy["action"] == "AUTO_APPROVE"
    assert json.loads(policy["object_types_json"]) == ["dict.table", "dict.column"]
    assert json.loads(policy["condition_json"]) == {"confidence_min": 0.90}
    # Must be evaluated before the unconditional catch-all so only
    # high-confidence dict objects ever reach AUTO_APPROVE.
    assert policy["priority"] < catch_all["priority"]


def test_low_confidence_dict_object_still_falls_through_to_require_human(tmp_path, monkeypatch):
    # Confirms the pre-existing catch-all policy is not weakened: an object
    # below the new policy's confidence threshold still requires a human.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.adf_placements", table_class="Master", row_count=50000, approved=False)
    _add_column(db, "dbo.adf_placements", "id")
    _add_domain(db, "dbo.adf_placements", "Student Lifecycle", confidence=0.3)
    _add_entity(db, "dbo.adf_placements", "Student", confidence=0.3)

    from data.governance_service import get_governance_profile
    profile = get_governance_profile(object_type="dict.table", source_id=1, table_fqn="dbo.adf_placements")
    assert profile.auto_approval_eligible is False
    assert profile.blocking_policy == "POLICY_REQUIRE_HUMAN_DICT_ENTRIES"
