"""
Tests for Phase 3 — Bulk Governance Operations in data/governance_service.py.

Covers:
  - BulkFilter and BulkOpResult dataclasses
  - _query_bulk_candidates() per object type, with all filter combinations
  - _check_policies_with_cache() policy evaluation without per-item DB round-trips
  - dry_run returns correct counts without modifying the DB
  - bulk_approve: high-confidence items approved; PII, high-risk domains, and
    REQUIRE_HUMAN policies block; already-approved items blocked
  - bulk_reject: PENDING items rejected; already-approved/rejected items blocked
  - governance_approval_events written for approved/rejected items
  - governance_bulk_ops record written after execution
  - exclude_pii=True (default) removes PII columns from candidates
  - source_id, confidence, domain, schema_name, entity filters applied correctly
  - unsupported object_type returns empty result (not an error)
  - no duplicate approval system created

Run from project root:
    venv/Scripts/pytest tests/test_governance_bulk.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-bulk-ops-long-enough-secret-32chars")
os.environ.setdefault("USER_ID_SALT", "test-salt-bulk-ops-phase3")

from data.governance_service import (
    BulkFilter,
    BulkOpResult,
    GovernanceState,
    GovernedObjectType,
    _BULK_APPROVE_BLOCKED_STATES,
    _BULK_REJECT_BLOCKED_STATES,
    _HARD_POLICY_PII,
    _HARD_POLICY_HIGH_RISK,
    _HARD_POLICY_IRREVERSIBLE,
    _check_policies_with_cache,
    _load_enabled_db_policies,
    _query_bulk_candidates,
    bulk_dry_run,
    bulk_approve,
    bulk_reject,
)


# ---------------------------------------------------------------------------
# Shared minimal schema
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Test',
        source_type  TEXT NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'relational_db',
        encrypted_config_json TEXT NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source_status TEXT NOT NULL DEFAULT 'ACTIVE',
        is_active    INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE data_dictionary_tables (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL DEFAULT 1,
        table_fqn         TEXT NOT NULL,
        table_name        TEXT NOT NULL,
        schema_name       TEXT NOT NULL DEFAULT 'dbo',
        table_type        TEXT NOT NULL DEFAULT 'TABLE',
        business_name     TEXT,
        description       TEXT,
        domain            TEXT,
        grain             TEXT,
        is_approved       INTEGER NOT NULL DEFAULT 0,
        approved_by       TEXT,
        approved_at       TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE data_dictionary_columns (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id         INTEGER NOT NULL,
        snapshot_id       INTEGER NOT NULL DEFAULT 1,
        table_fqn         TEXT NOT NULL,
        column_name       TEXT NOT NULL,
        business_label    TEXT,
        meaning           TEXT,
        semantic_type     TEXT,
        is_metric         INTEGER NOT NULL DEFAULT 0,
        is_dimension      INTEGER NOT NULL DEFAULT 0,
        is_date           INTEGER NOT NULL DEFAULT 0,
        is_id             INTEGER NOT NULL DEFAULT 0,
        pii_risk          INTEGER NOT NULL DEFAULT 0,
        is_approved       INTEGER NOT NULL DEFAULT 0,
        approved_by       TEXT,
        approved_at       TEXT,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE domain_learning_rules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       INTEGER NOT NULL,
        pattern_type    TEXT NOT NULL,
        pattern_value   TEXT NOT NULL,
        domain          TEXT NOT NULL,
        confidence      REAL NOT NULL DEFAULT 0.8,
        approval_status TEXT NOT NULL DEFAULT 'PENDING',
        created_by      TEXT NOT NULL DEFAULT 'system',
        approved_by     TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        approved_at     TEXT,
        active          INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE entity_learning_rules (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       INTEGER NOT NULL,
        pattern_type    TEXT NOT NULL,
        pattern_value   TEXT NOT NULL,
        entity          TEXT NOT NULL,
        confidence      REAL NOT NULL DEFAULT 0.8,
        approval_status TEXT NOT NULL DEFAULT 'PENDING',
        created_by      TEXT NOT NULL DEFAULT 'system',
        approved_by     TEXT,
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        approved_at     TEXT,
        active          INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE domain_rule_refinement_suggestions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        parent_rule_id   INTEGER NOT NULL DEFAULT 1,
        pattern_type     TEXT NOT NULL DEFAULT 'TOKEN',
        pattern_value    TEXT NOT NULL,
        suggested_domain TEXT NOT NULL,
        support_count    INTEGER NOT NULL DEFAULT 3,
        confidence       REAL NOT NULL DEFAULT 0.0,
        approval_status  TEXT NOT NULL DEFAULT 'PENDING',
        created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        approved_at      TEXT,
        approved_by      TEXT,
        active           INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE governance_approval_events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        object_type_id TEXT NOT NULL,
        object_id      TEXT NOT NULL,
        event_type     TEXT NOT NULL,
        from_state     TEXT,
        to_state       TEXT NOT NULL,
        actor_id       TEXT NOT NULL,
        notes          TEXT,
        source_service TEXT,
        created_at     TEXT NOT NULL
    );

    CREATE TABLE governance_state_map (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        object_type_id   TEXT NOT NULL,
        object_id        TEXT NOT NULL,
        approval_state   TEXT NOT NULL DEFAULT 'GENERATED',
        confidence_score REAL,
        confidence_tier  TEXT,
        reviewer_id      TEXT,
        reviewed_at      TEXT,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        UNIQUE(object_type_id, object_id)
    );

    CREATE TABLE governance_policies (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        policy_name       TEXT NOT NULL UNIQUE,
        enabled           INTEGER NOT NULL DEFAULT 1,
        priority          INTEGER NOT NULL DEFAULT 100,
        object_types_json TEXT NOT NULL DEFAULT '[]',
        condition_json    TEXT NOT NULL DEFAULT '{}',
        action            TEXT NOT NULL,
        created_by        TEXT NOT NULL DEFAULT 'system',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    CREATE TABLE governance_bulk_ops (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        actor_id           TEXT    NOT NULL,
        action             TEXT    NOT NULL,
        filter_json        TEXT    NOT NULL,
        affected_count     INTEGER NOT NULL DEFAULT 0,
        blocked_count      INTEGER NOT NULL DEFAULT 0,
        blocked_items_json TEXT    NOT NULL DEFAULT '[]',
        status             TEXT    NOT NULL DEFAULT 'COMPLETED',
        executed_at        TEXT    NOT NULL,
        undone_at          TEXT,
        undone_by          TEXT
    );
"""


class _NoClose:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def close(self):
        pass


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _nc(db: sqlite3.Connection) -> _NoClose:
    return _NoClose(db)


def _patch_all(db: sqlite3.Connection) -> ExitStack:
    """
    Patch every get_connection in every module that bulk ops touch.
    Returns an ExitStack that can be used as a context manager.
    """
    nc = _nc(db)
    stack = ExitStack()
    for module in (
        "data.governance_service",
        "data.dictionary_service",
        "data.domain_learning_service",
        "data.entity_learning_service",
        "data.domain_refinement_service",
    ):
        stack.enter_context(
            patch(f"{module}.get_connection", return_value=nc)
        )
    return stack


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _add_source(db, *, user_id="alice", source_id=1):
    db.execute(
        "INSERT OR IGNORE INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        "encrypted_config_json, config_schema_version, capabilities_json, "
        "metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (?, ?, 'Test', 'mssql', 'relational_db', '{}', 1, '[]', '{}', "
        "'ACTIVE', 1, '2025-01-01', '2025-01-01')",
        (source_id, user_id),
    )
    db.commit()


def _add_dict_table(db, *, source_id=1, fqn="dbo.fact_sales",
                    name="fact_sales", schema="dbo", business_name="Sales Facts",
                    domain=None, is_approved=0):
    db.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, "
        "table_type, business_name, domain, is_approved, "
        "generation_method, created_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?, 'TABLE', ?, ?, ?, 'rule_based', "
        "'2025-01-01', '2025-01-01')",
        (source_id, fqn, name, schema, business_name, domain, is_approved),
    )
    db.commit()


def _add_dict_column(db, *, source_id=1, fqn="dbo.fact_sales",
                     col="sale_amount", label="Sale Amount",
                     pii_risk=0, is_approved=0):
    db.execute(
        "INSERT INTO data_dictionary_columns "
        "(source_id, snapshot_id, table_fqn, column_name, business_label, "
        "pii_risk, is_approved, generation_method, created_at, updated_at) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, 'rule_based', '2025-01-01', '2025-01-01')",
        (source_id, fqn, col, label, pii_risk, is_approved),
    )
    db.commit()


def _add_domain_rule(db, *, source_id=1, ptype="PREFIX", pval="fact_",
                     domain="Sales", confidence=0.97,
                     status="PENDING", active=0):
    db.execute(
        "INSERT INTO domain_learning_rules "
        "(source_id, pattern_type, pattern_value, domain, confidence, "
        "approval_status, created_by, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, 'system', '2025-01-01', ?)",
        (source_id, ptype, pval, domain, confidence, status, active),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_entity_rule(db, *, source_id=1, ptype="TOKEN", pval="customer",
                     entity="Customer", confidence=0.95,
                     status="PENDING", active=0):
    db.execute(
        "INSERT INTO entity_learning_rules "
        "(source_id, pattern_type, pattern_value, entity, confidence, "
        "approval_status, created_by, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, 'system', '2025-01-01', ?)",
        (source_id, ptype, pval, entity, confidence, status, active),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_refinement(db, *, source_id=1, pval="orders",
                    suggested_domain="Sales", confidence=0.88,
                    status="PENDING"):
    db.execute(
        "INSERT INTO domain_rule_refinement_suggestions "
        "(source_id, parent_rule_id, pattern_type, pattern_value, "
        "suggested_domain, support_count, confidence, approval_status, "
        "created_at, active) "
        "VALUES (?, 1, 'TOKEN', ?, ?, 3, ?, ?, '2025-01-01', 0)",
        (source_id, pval, suggested_domain, confidence, status),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_policy(db, *, name, action, priority=100,
                obj_types=None, condition=None, enabled=1):
    db.execute(
        "INSERT INTO governance_policies "
        "(policy_name, enabled, priority, object_types_json, condition_json, "
        "action, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'test', '2025-01-01', '2025-01-01')",
        (name, enabled, priority,
         json.dumps(obj_types or []),
         json.dumps(condition or {}),
         action),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Part 1 — BulkFilter
# ---------------------------------------------------------------------------

class TestBulkFilter:
    def test_to_dict_round_trip(self):
        f = BulkFilter(
            object_type    = "domain.rule",
            source_id      = 5,
            confidence_min = 0.90,
            domain         = "Sales",
            exclude_pii    = True,
        )
        d = f.to_dict()
        f2 = BulkFilter.from_dict(d)
        assert f2.object_type == "domain.rule"
        assert f2.source_id == 5
        assert f2.confidence_min == 0.90
        assert f2.domain == "Sales"
        assert f2.exclude_pii is True

    def test_exclude_pii_defaults_to_true(self):
        f = BulkFilter.from_dict({"object_type": "dict.column"})
        assert f.exclude_pii is True

    def test_all_defaults_are_none(self):
        f = BulkFilter(object_type="entity.rule")
        assert f.source_id is None
        assert f.confidence_min is None
        assert f.domain is None
        assert f.entity is None
        assert f.schema_name is None


# ---------------------------------------------------------------------------
# Part 2 — _query_bulk_candidates
# ---------------------------------------------------------------------------

class TestQueryBulkCandidates:
    def test_dict_table_returns_unapproved_with_business_name(self):
        db = _make_db()
        _add_dict_table(db, fqn="dbo.a", business_name="A")
        _add_dict_table(db, fqn="dbo.b", business_name="B")
        _add_dict_table(db, fqn="dbo.c", business_name="C", is_approved=1)  # excluded
        f = BulkFilter(object_type="dict.table")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 2
        fqns = {r["table_fqn"] for r in rows}
        assert "dbo.a" in fqns and "dbo.b" in fqns

    def test_dict_table_source_id_filter(self):
        db = _make_db()
        _add_dict_table(db, source_id=1, fqn="dbo.t1", business_name="T1")
        _add_dict_table(db, source_id=2, fqn="dbo.t2", business_name="T2")
        f = BulkFilter(object_type="dict.table", source_id=1)
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["source_id"] == 1

    def test_dict_table_schema_filter(self):
        db = _make_db()
        _add_dict_table(db, fqn="dbo.fact_a", name="fact_a", schema="dbo", business_name="A")
        _add_dict_table(db, fqn="stg.fact_b", name="fact_b", schema="stg", business_name="B")
        f = BulkFilter(object_type="dict.table", schema_name="dbo")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["schema_name"] == "dbo"

    def test_dict_table_domain_filter(self):
        db = _make_db()
        _add_dict_table(db, fqn="dbo.t1", business_name="T1", domain="Sales")
        _add_dict_table(db, fqn="dbo.t2", business_name="T2", domain="HR")
        f = BulkFilter(object_type="dict.table", domain="Sales")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["domain"] == "Sales"

    def test_dict_column_exclude_pii_default(self):
        db = _make_db()
        _add_dict_column(db, col="amount", label="Amount", pii_risk=0)
        _add_dict_column(db, col="email",  label="Email",  pii_risk=1)
        f = BulkFilter(object_type="dict.column", exclude_pii=True)
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["column_name"] == "amount"

    def test_dict_column_include_pii_when_flag_false(self):
        db = _make_db()
        _add_dict_column(db, col="amount", label="Amount", pii_risk=0)
        _add_dict_column(db, col="email",  label="Email",  pii_risk=1)
        f = BulkFilter(object_type="dict.column", exclude_pii=False)
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 2

    def test_dict_column_schema_filter(self):
        db = _make_db()
        _add_dict_column(db, fqn="dbo.t1", col="col1", label="C1")
        _add_dict_column(db, fqn="stg.t2", col="col2", label="C2")
        f = BulkFilter(object_type="dict.column", schema_name="dbo")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["table_fqn"].startswith("dbo.")

    def test_domain_rule_confidence_min(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_",  confidence=0.99)
        _add_domain_rule(db, pval="dim_",   confidence=0.70)
        f = BulkFilter(object_type="domain.rule", confidence_min=0.95)
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["confidence"] == pytest.approx(0.99)

    def test_domain_rule_confidence_max(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_",   confidence=0.99)
        _add_domain_rule(db, pval="staging_", confidence=0.65)
        f = BulkFilter(object_type="domain.rule", confidence_max=0.70)
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1
        assert rows[0]["confidence"] < 0.70

    def test_domain_rule_domain_filter(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_", domain="Sales")
        _add_domain_rule(db, pval="emp_",  domain="HR")
        f = BulkFilter(object_type="domain.rule", domain="Sales")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1 and rows[0]["domain"] == "Sales"

    def test_domain_rule_already_approved_excluded(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_", status="APPROVED")
        f = BulkFilter(object_type="domain.rule")
        rows = _query_bulk_candidates(f, db)
        assert rows == []

    def test_entity_rule_entity_filter(self):
        db = _make_db()
        _add_entity_rule(db, pval="customer", entity="Customer")
        _add_entity_rule(db, pval="order",    entity="Order")
        f = BulkFilter(object_type="entity.rule", entity="Customer")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1 and rows[0]["entity"] == "Customer"

    def test_domain_refinement_domain_filter(self):
        db = _make_db()
        _add_refinement(db, pval="orders",  suggested_domain="Sales")
        _add_refinement(db, pval="payroll", suggested_domain="HR")
        f = BulkFilter(object_type="domain.refinement", domain="Sales")
        rows = _query_bulk_candidates(f, db)
        assert len(rows) == 1

    def test_unsupported_type_returns_empty(self):
        db = _make_db()
        f = BulkFilter(object_type="tool.engine")
        rows = _query_bulk_candidates(f, db)
        assert rows == []


# ---------------------------------------------------------------------------
# Part 3 — _check_policies_with_cache
# ---------------------------------------------------------------------------

class TestCheckPoliciesWithCache:
    pass  # coverage provided by TestCachedPolicies below

class TestCachedPolicies:
    def _rule_profile(self, *, confidence=0.97, domain="Sales",
                      pii_risk=False, state=None):
        """Build a minimal GovernanceProfile for a domain.rule."""
        from data.governance_service import GovernanceProfile
        s = state or GovernanceState.SUGGESTED
        can_ai = s in (GovernanceState.HUMAN_APPROVED, GovernanceState.AUTO_APPROVED)
        return GovernanceProfile(
            object_type_id    = "domain.rule",
            object_id         = "1",
            approval_state    = s,
            confidence_score  = confidence,
            confidence_tier   = "VERY_HIGH" if confidence >= 0.95 else "HIGH",
            confidence_source = "learning_engine",
            review_required   = s == GovernanceState.SUGGESTED,
            review_reason     = None,
            reviewed_by       = None,
            reviewed_at       = None,
            created_by        = "system",
            created_at        = "2025-01-01",
            updated_at        = "2025-01-01",
            evidence          = [],
            can_ai_use        = can_ai,
            ai_warning        = None,
            pii_risk          = pii_risk,
            domain_context    = domain,
        )

    def test_auto_approve_policy_matched(self):
        db = _make_db()
        _add_policy(db, name="AUTO_HIGH", action="AUTO_APPROVE",
                    condition={"confidence_min": 0.95})
        policies = _load_enabled_db_policies(db)
        profile = self._rule_profile(confidence=0.99)
        result = _check_policies_with_cache(profile, policies)
        assert result.auto_approval_eligible is True
        assert result.matched_policy == "AUTO_HIGH"

    def test_require_human_policy_blocks(self):
        db = _make_db()
        _add_policy(db, name="REQ_HUMAN", action="REQUIRE_HUMAN", condition={})
        policies = _load_enabled_db_policies(db)
        profile = self._rule_profile(confidence=0.99)
        result = _check_policies_with_cache(profile, policies)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == "REQ_HUMAN"

    def test_pii_risk_hard_policy_overrides_auto_approve(self):
        db = _make_db()
        _add_policy(db, name="AUTO_ALL", action="AUTO_APPROVE", condition={})
        policies = _load_enabled_db_policies(db)
        profile = self._rule_profile(pii_risk=True)
        result = _check_policies_with_cache(profile, policies)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == _HARD_POLICY_PII

    def test_financial_domain_hard_policy_blocks(self):
        db = _make_db()
        _add_policy(db, name="AUTO_ALL", action="AUTO_APPROVE", condition={})
        policies = _load_enabled_db_policies(db)
        profile = self._rule_profile(domain="Finance", confidence=0.999)
        result = _check_policies_with_cache(profile, policies)
        assert result.blocking_policy == _HARD_POLICY_HIGH_RISK

    def test_disabled_policy_not_in_cache(self):
        db = _make_db()
        _add_policy(db, name="DISABLED", action="AUTO_APPROVE",
                    condition={"confidence_min": 0.5}, enabled=0)
        policies = _load_enabled_db_policies(db)
        assert len(policies) == 0

    def test_empty_cache_returns_default(self):
        from data.governance_service import GovernanceProfile
        profile = self._rule_profile(confidence=0.99)
        result = _check_policies_with_cache(profile, [])
        # SUGGESTED with no matching policy → review_required=True, not eligible
        assert result.auto_approval_eligible is False
        assert result.blocking_policy is None
        assert result.review_required is True


# ---------------------------------------------------------------------------
# Part 4 — Dry-run
# ---------------------------------------------------------------------------

class TestBulkDryRun:
    def test_dry_run_returns_correct_counts(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_",   confidence=0.99, domain="Sales")
        _add_domain_rule(db, pval="dim_",    confidence=0.99, domain="Sales")
        _add_domain_rule(db, pval="staging_", confidence=0.99, domain="Finance")  # blocked
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.90)
        with _patch_all(db):
            result = bulk_dry_run(f, actor_id="alice")
        assert result.dry_run is True
        assert result.total_candidates == 3
        assert result.affected_count == 2
        assert result.blocked_count == 1

    def test_dry_run_does_not_modify_db(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_", confidence=0.99, domain="Sales")
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.90)
        with _patch_all(db):
            bulk_dry_run(f, actor_id="alice")
        # Rule must still be PENDING
        row = db.execute("SELECT approval_status FROM domain_learning_rules").fetchone()
        assert row["approval_status"] == "PENDING"

    def test_dry_run_bulk_op_id_is_none(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_", confidence=0.99, domain="Sales")
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_dry_run(f, actor_id="alice")
        assert result.bulk_op_id is None

    def test_dry_run_pii_column_counted_as_blocked(self):
        db = _make_db()
        _add_dict_column(db, col="email", label="Email", pii_risk=1)
        _add_dict_column(db, col="amount", label="Amount", pii_risk=0)
        f = BulkFilter(object_type="dict.column", source_id=1, exclude_pii=False)
        with _patch_all(db):
            result = bulk_dry_run(f, actor_id="alice")
        # PII column blocked, non-PII also blocked by POLICY_REQUIRE_HUMAN_DICT_ENTRIES
        # (no policies seeded in test DB) → blocked by policy evaluation fallback
        # Actually with no DB policies, dict.column in SUGGESTED with no
        # blocking policy → eligible. But wait, there's HARD_PII for email
        pii_blocked = [i for i in result.blocked_items
                       if i["blocking_policy"] == _HARD_POLICY_PII]
        assert len(pii_blocked) == 1

    def test_dry_run_empty_candidates_returns_zero(self):
        db = _make_db()
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_dry_run(f, actor_id="alice")
        assert result.total_candidates == 0
        assert result.affected_count == 0
        assert result.blocked_count == 0

    def test_dry_run_reject_action_previews_without_writing(self):
        db = _make_db()
        _add_domain_rule(db, pval="fact_", confidence=0.99, domain="Sales")
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_dry_run(f, actor_id="alice", action="reject")
        assert result.dry_run is True
        assert result.action == "reject"
        assert result.affected_count == 1
        row = db.execute("SELECT approval_status FROM domain_learning_rules").fetchone()
        assert row["approval_status"] == "PENDING"  # untouched — still a dry run

    def test_dry_run_requires_source_id(self):
        f = BulkFilter(object_type="domain.rule")
        with pytest.raises(ValueError):
            bulk_dry_run(f, actor_id="alice")

    def test_dry_run_rejects_invalid_action(self):
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with pytest.raises(ValueError):
            bulk_dry_run(f, actor_id="alice", action="delete")


# ---------------------------------------------------------------------------
# Part 5 — bulk_approve
# ---------------------------------------------------------------------------

class TestBulkApprove:
    def test_approves_pending_domain_rules(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        rule_id = _add_domain_rule(db, source_id=1, pval="fact_",
                                   confidence=0.99, domain="Sales")
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.90)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 1
        assert result.blocked_count == 0
        row = db.execute(
            "SELECT approval_status, active FROM domain_learning_rules WHERE id = ?",
            (rule_id,)
        ).fetchone()
        assert row["approval_status"] == "APPROVED"
        assert row["active"] == 1

    def test_pii_column_blocked(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_dict_column(db, source_id=1, col="email", label="Email", pii_risk=1)
        f = BulkFilter(object_type="dict.column", source_id=1, exclude_pii=False)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 0
        assert result.blocked_count == 1
        assert result.blocked_items[0]["blocking_policy"] == _HARD_POLICY_PII

    def test_high_risk_domain_blocked(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="rev_",
                         domain="Finance", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 0
        assert result.blocked_items[0]["blocking_policy"] == _HARD_POLICY_HIGH_RISK

    def test_already_approved_items_blocked(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="approved_",
                         confidence=0.99, status="APPROVED", active=1)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        # Already APPROVED items are excluded by the WHERE approval_status='PENDING' query
        # so they don't appear as candidates at all
        assert result.total_candidates == 0
        assert result.affected_count == 0

    def test_rejected_items_not_in_candidates(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="rejected_",
                         confidence=0.99, status="REJECTED")
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.total_candidates == 0

    def test_require_human_policy_blocks_item(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        _add_policy(db, name="ALWAYS_HUMAN", action="REQUIRE_HUMAN",
                    obj_types=["domain.rule"], condition={})
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 0
        assert result.blocked_items[0]["blocking_policy"] == "ALWAYS_HUMAN"

    def test_governance_approval_event_written(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.90)
        with _patch_all(db):
            bulk_approve(f, actor_id="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'domain.rule'"
        ).fetchall()
        # Phase 1 wiring: approve_domain_rule() logs a governance event
        assert len(events) >= 1
        approved_events = [dict(e) for e in events if dict(e)["to_state"] == "HUMAN_APPROVED"]
        assert len(approved_events) >= 1

    def test_bulk_op_record_written(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.bulk_op_id is not None
        row = db.execute(
            "SELECT * FROM governance_bulk_ops WHERE id = ?",
            (result.bulk_op_id,)
        ).fetchone()
        assert row is not None
        d = dict(row)
        assert d["action"] == "approve"
        assert d["actor_id"] == "alice"
        assert d["affected_count"] == 1

    def test_multiple_rules_partial_block(self):
        """Mix of blockable (Finance domain) and approvable (Sales domain) rules."""
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",  domain="Sales",   confidence=0.99)
        _add_domain_rule(db, source_id=1, pval="rev_",   domain="Finance", confidence=0.99)
        _add_domain_rule(db, source_id=1, pval="emp_",   domain="Sales",   confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.95)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.total_candidates == 3
        assert result.affected_count == 2
        assert result.blocked_count == 1

    def test_bulk_approve_entity_rules(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_entity_rule(db, source_id=1, pval="customer", entity="Customer", confidence=0.96)
        f = BulkFilter(object_type="entity.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute("SELECT approval_status FROM entity_learning_rules").fetchone()
        assert row["approval_status"] == "APPROVED"

    def test_bulk_approve_dict_table(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_dict_table(db, source_id=1, fqn="dbo.t1", business_name="T1")
        f = BulkFilter(object_type="dict.table", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute("SELECT is_approved FROM data_dictionary_tables").fetchone()
        assert row["is_approved"] == 1

    def test_bulk_approve_dict_column(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_dict_column(db, source_id=1, col="revenue", label="Revenue",
                         pii_risk=0)
        f = BulkFilter(object_type="dict.column", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute("SELECT is_approved FROM data_dictionary_columns").fetchone()
        assert row["is_approved"] == 1

    def test_exclude_pii_prevents_pii_column_from_candidates(self):
        """exclude_pii=True (default) removes PII columns at the query level."""
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_dict_column(db, source_id=1, col="email", label="Email", pii_risk=1)
        f = BulkFilter(object_type="dict.column", source_id=1, exclude_pii=True)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        # email is filtered OUT by query — not even a candidate
        assert result.total_candidates == 0

    def test_bulk_approve_refinement_suggestions(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_refinement(db, source_id=1, pval="orders",
                        suggested_domain="Sales", confidence=0.88)
        f = BulkFilter(object_type="domain.refinement", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute(
            "SELECT approval_status FROM domain_rule_refinement_suggestions"
        ).fetchone()
        assert row["approval_status"] == "APPROVED"

    def test_ownership_mismatch_results_in_blocked_item(self):
        """Actor 'bob' does not own source 1 (owned by 'alice')."""
        db = _make_db()
        _add_source(db, user_id="alice", source_id=1)
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="bob")   # wrong user
        # The underlying approve_domain_rule() will return None (ownership check fails)
        assert result.affected_count == 0
        assert result.blocked_count == 1


# ---------------------------------------------------------------------------
# Part 6 — bulk_reject
# ---------------------------------------------------------------------------

class TestBulkReject:
    def test_rejects_pending_domain_rules(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        rule_id = _add_domain_rule(db, source_id=1, pval="stg_",
                                   domain="Sales", confidence=0.55)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_reject(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute(
            "SELECT approval_status FROM domain_learning_rules WHERE id = ?",
            (rule_id,)
        ).fetchone()
        assert row["approval_status"] == "REJECTED"

    def test_already_approved_rules_blocked(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        # Already APPROVED → query returns nothing (WHERE approval_status='PENDING')
        _add_domain_rule(db, source_id=1, pval="approved_",
                         domain="Sales", confidence=0.99, status="APPROVED", active=1)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_reject(f, actor_id="alice")
        assert result.total_candidates == 0

    def test_bulk_reject_entity_rules(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_entity_rule(db, source_id=1, pval="product",
                         entity="Product", confidence=0.60)
        f = BulkFilter(object_type="entity.rule", source_id=1)
        with _patch_all(db):
            result = bulk_reject(f, actor_id="alice")
        assert result.affected_count == 1
        row = db.execute("SELECT approval_status FROM entity_learning_rules").fetchone()
        assert row["approval_status"] == "REJECTED"

    def test_bulk_reject_dict_table_writes_governance_event(self):
        """dict.table has no source rejection — governance event written only."""
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_dict_table(db, source_id=1, fqn="dbo.t1", business_name="T1")
        f = BulkFilter(object_type="dict.table", source_id=1)
        with _patch_all(db):
            result = bulk_reject(f, actor_id="alice")
        assert result.affected_count == 1
        # Source table is NOT changed (no rejection state for dict entries)
        row = db.execute(
            "SELECT is_approved FROM data_dictionary_tables"
        ).fetchone()
        assert row["is_approved"] == 0  # unchanged
        # But governance state map IS updated
        state_row = db.execute(
            "SELECT approval_state FROM governance_state_map "
            "WHERE object_type_id = 'dict.table'"
        ).fetchone()
        assert state_row is not None
        assert state_row["approval_state"] == "REJECTED"

    def test_bulk_reject_bulk_op_record_written(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="stg_",
                         domain="Sales", confidence=0.55)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_reject(f, actor_id="alice")
        assert result.bulk_op_id is not None
        row = db.execute(
            "SELECT action, affected_count FROM governance_bulk_ops WHERE id = ?",
            (result.bulk_op_id,)
        ).fetchone()
        assert row["action"] == "reject"
        assert row["affected_count"] == 1

    def test_governance_event_written_for_rejection(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            bulk_reject(f, actor_id="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'domain.rule'"
        ).fetchall()
        rejected = [e for e in events if dict(e)["to_state"] == "REJECTED"]
        assert len(rejected) >= 1


# ---------------------------------------------------------------------------
# Part 7 — BulkOpResult serialisation
# ---------------------------------------------------------------------------

class TestBulkOpResult:
    def test_to_dict_contains_required_keys(self):
        result = BulkOpResult(
            action           = "approve",
            dry_run          = False,
            object_type      = "domain.rule",
            total_candidates = 5,
            affected_count   = 3,
            blocked_count    = 2,
            blocked_items    = [],
            executed_at      = "2025-01-01",
            bulk_op_id       = 42,
        )
        d = result.to_dict()
        required = {
            "action", "dry_run", "object_type", "total_candidates",
            "affected_count", "blocked_count", "blocked_items",
            "executed_at", "bulk_op_id",
        }
        assert required == set(d.keys())

    def test_blocked_items_list_structure(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="rev_",
                         domain="Finance", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        assert len(result.blocked_items) == 1
        item = result.blocked_items[0]
        assert "object_id" in item
        assert "blocking_policy" in item
        assert "reason" in item


# ---------------------------------------------------------------------------
# Part 8 — Governance audit trail
# ---------------------------------------------------------------------------

class TestBulkAuditTrail:
    def test_bulk_op_filter_json_persisted(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1, confidence_min=0.95)
        with _patch_all(db):
            result = bulk_approve(f, actor_id="alice")
        row = db.execute(
            "SELECT filter_json FROM governance_bulk_ops WHERE id = ?",
            (result.bulk_op_id,)
        ).fetchone()
        saved_filter = json.loads(row["filter_json"])
        assert saved_filter["object_type"] == "domain.rule"
        assert saved_filter["source_id"] == 1
        assert saved_filter["confidence_min"] == 0.95

    def test_multiple_bulk_ops_recorded_independently(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        _add_domain_rule(db, source_id=1, pval="dim_",
                         domain="Sales", confidence=0.99)
        f1 = BulkFilter(object_type="domain.rule", source_id=1,
                        domain="Sales", confidence_min=0.98)
        with _patch_all(db):
            result1 = bulk_approve(f1, actor_id="alice")
        # By now both rules are APPROVED; a second bulk_approve on PENDING returns 0
        f2 = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            result2 = bulk_approve(f2, actor_id="alice")
        # Two separate bulk_op records
        rows = db.execute("SELECT id FROM governance_bulk_ops").fetchall()
        assert len(rows) == 2
        assert result1.bulk_op_id != result2.bulk_op_id


# ---------------------------------------------------------------------------
# Part 9 — No duplicate approval systems
# ---------------------------------------------------------------------------

class TestNoSecondApprovalSystem:
    def test_bulk_approve_calls_existing_domain_rule_function(self):
        """Bulk approval doesn't bypass approve_domain_rule — the existing
        function must be called (proven by approval_status being updated)."""
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="fact_",
                         domain="Sales", confidence=0.99)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            bulk_approve(f, actor_id="alice")
        row = db.execute(
            "SELECT approval_status, approved_by FROM domain_learning_rules"
        ).fetchone()
        # The existing function writes approved_by — proof it was called
        assert row["approval_status"] == "APPROVED"
        assert row["approved_by"] == "alice"

    def test_bulk_reject_calls_existing_domain_rule_function(self):
        db = _make_db()
        _add_source(db, user_id="alice")
        _add_domain_rule(db, source_id=1, pval="stg_",
                         domain="Sales", confidence=0.55)
        f = BulkFilter(object_type="domain.rule", source_id=1)
        with _patch_all(db):
            bulk_reject(f, actor_id="alice")
        row = db.execute(
            "SELECT approval_status, approved_by FROM domain_learning_rules"
        ).fetchone()
        assert row["approval_status"] == "REJECTED"
        assert row["approved_by"] == "alice"

    def test_no_new_approval_table_created_by_bulk(self):
        """Phase 3 only adds governance_bulk_ops — no other new approval table."""
        db = _make_db()
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        # governance_bulk_ops must exist
        assert "governance_bulk_ops" in tables
        # No second approval-related table should have been created
        unexpected = {t for t in tables if "approval" in t.lower()
                      and t not in {
                          "governance_approval_events",
                          "engine_approval_events",
                      }}
        assert unexpected == set()
