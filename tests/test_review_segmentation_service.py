"""
Tests for data.review_segmentation_service — CCPP Semantic Governance
Activation (Milestone M-3, Part 3).

Two layers:
  1. Pure unit tests of classify_asset() — precedence ordering, one fixture
     per group, PII always wins, human-override respects existing approval.
  2. An end-to-end test of segment_source_assets() against a small real
     schema (data.models.init_db), proving the read-only aggregation works
     against the actual production table shapes, not a hand-rolled stub.
"""
import os
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-review-segmentation-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-review-segmentation-salt-long-enough-1")

import data.models as models
from data.review_segmentation_service import classify_asset, segment_source_assets

_NOW = "2026-07-12T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 1. Pure classify_asset() unit tests
# ---------------------------------------------------------------------------

def test_pii_always_wins_over_every_other_signal():
    group = classify_asset(
        table_class="Master", table_type="TABLE", is_approved=True,
        domain="Finance", entity="Payment", has_pii=True,
        relationship_count=5, naming_hits=set(),
    )
    assert group == "F"


def test_approved_with_domain_entity_is_human_override_into_a():
    group = classify_asset(
        table_class=None, table_type="TABLE", is_approved=True,
        domain="Finance", entity="Payment", has_pii=False,
        relationship_count=0, naming_hits={"temp"},  # naming hit ignored — human override
    )
    assert group == "A"


def test_staging_table_class_or_naming_is_group_d():
    assert classify_asset(
        table_class="Staging", table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits=set(),
    ) == "D"
    assert classify_asset(
        table_class=None, table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits={"staging"},
    ) == "D"


def test_audit_table_class_or_framework_naming_is_group_e():
    assert classify_asset(
        table_class="Audit", table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits=set(),
    ) == "E"
    assert classify_asset(
        table_class=None, table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits={"generated"},
    ) == "E"
    assert classify_asset(
        table_class=None, table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits={"zoominfo"},
    ) == "E"


def test_historical_naming_or_dated_copy_is_group_c():
    assert classify_asset(
        table_class=None, table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits={"archive"},
    ) == "C"
    assert classify_asset(
        table_class=None, table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits={"dated_copy"},
    ) == "C"


def test_reporting_table_class_or_view_is_group_b():
    assert classify_asset(
        table_class="Reporting", table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits=set(),
    ) == "B"
    assert classify_asset(
        table_class=None, table_type="VIEW", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits=set(),
    ) == "B"


def test_unapproved_master_with_domain_entity_is_candidate_authoritative_a():
    group = classify_asset(
        table_class="Master", table_type="TABLE", is_approved=False,
        domain="Finance", entity="Payment", has_pii=False,
        relationship_count=3, naming_hits=set(),
    )
    assert group == "A"


def test_ambiguous_evidence_falls_back_to_g():
    group = classify_asset(
        table_class="Unknown", table_type="TABLE", is_approved=False,
        domain=None, entity=None, has_pii=False,
        relationship_count=0, naming_hits=set(),
    )
    assert group == "G"


def test_negative_naming_takes_precedence_over_unapproved_master_class():
    # Master class alone (no approval, no naming penalty) would be A; a
    # staging-name hit on the same table pulls it to D instead — naming still
    # matters when there's no human approval to override it.
    group = classify_asset(
        table_class="Master", table_type="TABLE", is_approved=False,
        domain="Finance", entity="Payment", has_pii=False,
        relationship_count=0, naming_hits={"staging"},
    )
    assert group == "D"


# ---------------------------------------------------------------------------
# 2. segment_source_assets() end-to-end against the real schema
# ---------------------------------------------------------------------------

def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "segmentation.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

    for mod in ("data.review_segmentation_service", "data.business_knowledge_service"):
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL_DB','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',3,'{}',?,?)",
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


def _insert_dict_table(db_path, table_fqn, *, is_approved=0):
    schema_name, table_name = table_fqn.split(".", 1)
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, is_approved, "
        " generation_method, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, ?, 'BASE TABLE', ?, 'rule_based', ?, ?)",
        (table_fqn, table_name, schema_name, is_approved, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_table_profile(db_path, table_fqn, *, table_class="Unknown"):
    schema_name, table_name = table_fqn.split(".", 1)
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO profiling_table_profiles "
        "(profiling_snapshot_id, source_id, table_fqn, table_name, schema_name, table_class, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, ?, ?, ?, ?)",
        (table_fqn, table_name, schema_name, table_class, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def _insert_domain_entity(db_path, table_fqn, domain, entity):
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO domain_assignments (source_id, profiling_snapshot_id, table_fqn, domain, confidence, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, 0.9, ?, ?)",
        (table_fqn, domain, _NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO entity_assignments (source_id, profiling_snapshot_id, table_fqn, entity, confidence, created_at, updated_at) "
        "VALUES (1, 1, ?, ?, 0.9, ?, ?)",
        (table_fqn, entity, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def test_segment_source_assets_buckets_real_tables(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)

    # A: approved Master table with domain/entity.
    _insert_dict_table(db, "dbo.clients", is_approved=1)
    _insert_table_profile(db, "dbo.clients", table_class="Master")
    _insert_domain_entity(db, "dbo.clients", "Finance", "Client")

    # D: unapproved staging-named table.
    _insert_dict_table(db, "dbo.clients_staging")
    _insert_table_profile(db, "dbo.clients_staging", table_class="Unknown")

    # G: unapproved, no signal at all.
    _insert_dict_table(db, "dbo.misc_widgets")
    _insert_table_profile(db, "dbo.misc_widgets", table_class="Unknown")

    result = segment_source_assets(1, "u1")

    assert result["total_tables"] == 3
    assert result["groups"]["A"]["count"] == 1
    assert "dbo.clients" in result["groups"]["A"]["example_tables"]
    assert result["groups"]["D"]["count"] == 1
    assert "dbo.clients_staging" in result["groups"]["D"]["example_tables"]
    assert result["groups"]["G"]["count"] == 1
    assert "dbo.misc_widgets" in result["groups"]["G"]["example_tables"]


def test_segment_source_assets_returns_none_for_unowned_source(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    assert segment_source_assets(1, "someone-else") is None
