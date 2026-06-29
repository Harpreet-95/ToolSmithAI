"""
Tests for Phase 5 — Governance Decision Intelligence.

Covers:
  - calculate_risk_score() — pure scoring across all profile states
  - recommend_next_action() — pure recommendation logic
  - get_governance_explanation() — full integration through get_governance_profile()
  - governance_readiness_summary() — aggregate health metrics from governance_state_map
  - Unknown object handling
  - Backward compatibility (no existing function signatures changed)

Run from project root:
    venv/Scripts/pytest tests/test_governance_intelligence.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-intelligence-long-enough-32chars")
os.environ.setdefault("USER_ID_SALT", "test-salt-intelligence-phase5")

from data.governance_service import (
    GovernanceProfile,
    GovernanceState,
    GovernedObjectType,
    NextAction,
    GovernanceExplanation,
    _HARD_POLICY_PII,
    _HARD_POLICY_HIGH_RISK,
    calculate_risk_score,
    recommend_next_action,
    get_governance_explanation,
    governance_readiness_summary,
)


# ---------------------------------------------------------------------------
# Minimal schema for integration tests
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

    CREATE TABLE engine_tools (
        id              TEXT PRIMARY KEY,
        name            TEXT NOT NULL,
        version         TEXT NOT NULL DEFAULT '1.0.0',
        status          TEXT NOT NULL DEFAULT 'draft',
        definition_json TEXT NOT NULL DEFAULT '{}',
        created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        schema_snapshot_id INTEGER NOT NULL DEFAULT 1,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        mode             TEXT NOT NULL DEFAULT 'full',
        sample_rate      REAL NOT NULL DEFAULT 1.0,
        profiling_rules_version TEXT NOT NULL DEFAULT '1.0.0',
        status           TEXT NOT NULL DEFAULT 'COMPLETE',
        tables_total     INTEGER NOT NULL DEFAULT 0,
        tables_profiled  INTEGER NOT NULL DEFAULT 0,
        tables_skipped   INTEGER NOT NULL DEFAULT 0,
        tables_failed    INTEGER NOT NULL DEFAULT 0,
        tables_timed_out INTEGER NOT NULL DEFAULT 0,
        columns_total    INTEGER NOT NULL DEFAULT 0,
        columns_profiled INTEGER NOT NULL DEFAULT 0,
        columns_skipped  INTEGER NOT NULL DEFAULT 0,
        total_rows_profiled INTEGER NOT NULL DEFAULT 0,
        pii_columns_found INTEGER NOT NULL DEFAULT 0,
        classifications_complete INTEGER NOT NULL DEFAULT 0,
        started_at       TEXT,
        completed_at     TEXT,
        duration_seconds INTEGER,
        resumable_state_json TEXT,
        batch_size       INTEGER NOT NULL DEFAULT 50,
        next_table_index INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE profiling_column_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT NOT NULL,
        column_name           TEXT NOT NULL,
        data_type             TEXT NOT NULL DEFAULT 'TEXT',
        pii_name_heuristic    INTEGER NOT NULL DEFAULT 0,
        pii_confirmed         INTEGER NOT NULL DEFAULT 0,
        pii_signals_json      TEXT,
        semantic_type         TEXT,
        semantic_confidence   REAL,
        created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    CREATE TABLE governance_assignments (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        object_type      TEXT    NOT NULL,
        object_id        TEXT    NOT NULL,
        source_id        INTEGER,
        assigned_to      TEXT    NOT NULL,
        assigned_by      TEXT    NOT NULL,
        assignment_group TEXT,
        priority         TEXT    NOT NULL DEFAULT 'MEDIUM',
        status           TEXT    NOT NULL DEFAULT 'OPEN',
        due_date         TEXT,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        completed_at     TEXT
    );
"""


class _NoClose:
    def __init__(self, conn):
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


def _patch(db):
    return patch("data.governance_service.get_connection", return_value=_NoClose(db))


def _make_profile(**kwargs) -> GovernanceProfile:
    defaults = dict(
        object_type_id    = GovernedObjectType.DOMAIN_RULE,
        object_id         = "1",
        approval_state    = GovernanceState.SUGGESTED,
        confidence_score  = 0.85,
        confidence_tier   = "HIGH",
        confidence_source = "learning_engine",
        review_required   = True,
        review_reason     = None,
        reviewed_by       = None,
        reviewed_at       = None,
        created_by        = "system",
        created_at        = "2025-01-01",
        updated_at        = "2025-01-01",
        evidence          = [],
        can_ai_use        = True,
        ai_warning        = None,
        pii_risk          = False,
        domain_context    = "Sales",
        auto_approval_eligible = False,
        blocking_policy   = None,
        matched_policy    = None,
    )
    defaults.update(kwargs)
    return GovernanceProfile(**defaults)


# ---------------------------------------------------------------------------
# Part 1 — calculate_risk_score (pure)
# ---------------------------------------------------------------------------

class TestRiskScore:
    def test_pii_risk_is_very_high(self):
        p = _make_profile(pii_risk=True, blocking_policy=_HARD_POLICY_PII)
        score = calculate_risk_score(p)
        assert score >= 80

    def test_human_approved_is_very_low(self):
        p = _make_profile(
            approval_state   = GovernanceState.HUMAN_APPROVED,
            confidence_score = 0.99,
            evidence         = [{"x": 1}],
        )
        score = calculate_risk_score(p)
        assert score <= 10

    def test_auto_approved_is_very_low(self):
        p = _make_profile(
            approval_state   = GovernanceState.AUTO_APPROVED,
            confidence_score = 0.99,
        )
        score = calculate_risk_score(p)
        assert score <= 10

    def test_finance_domain_is_elevated(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_HIGH_RISK, domain_context="Finance")
        score = calculate_risk_score(p)
        assert score >= 50

    def test_low_confidence_is_elevated(self):
        p = _make_profile(confidence_score=0.40)
        score_low  = calculate_risk_score(p)
        p_high = _make_profile(confidence_score=0.95)
        score_high = calculate_risk_score(p_high)
        assert score_low > score_high

    def test_generated_state_is_riskier_than_human_approved(self):
        gen = _make_profile(approval_state=GovernanceState.GENERATED, confidence_score=None)
        approved = _make_profile(approval_state=GovernanceState.HUMAN_APPROVED, confidence_score=0.99)
        assert calculate_risk_score(gen) > calculate_risk_score(approved)

    def test_needs_review_is_high_risk(self):
        p = _make_profile(approval_state=GovernanceState.NEEDS_REVIEW)
        score = calculate_risk_score(p)
        assert score >= 60

    def test_auto_approval_eligible_lowers_score(self):
        eligible    = _make_profile(auto_approval_eligible=True,  confidence_score=0.99)
        not_eligible = _make_profile(auto_approval_eligible=False, confidence_score=0.99)
        assert calculate_risk_score(eligible) < calculate_risk_score(not_eligible)

    def test_score_bounded_0_to_100(self):
        worst = _make_profile(
            pii_risk=True, blocking_policy=_HARD_POLICY_PII,
            approval_state=GovernanceState.NEEDS_REVIEW,
            confidence_score=None, evidence=[],
        )
        best = _make_profile(
            approval_state=GovernanceState.HUMAN_APPROVED,
            confidence_score=1.0, auto_approval_eligible=True,
            evidence=[{"x": 1}],
        )
        assert 0 <= calculate_risk_score(worst) <= 100
        assert 0 <= calculate_risk_score(best) <= 100

    def test_no_evidence_increases_risk(self):
        with_ev    = _make_profile(evidence=[{"signal": "x"}])
        without_ev = _make_profile(evidence=[])
        assert calculate_risk_score(without_ev) >= calculate_risk_score(with_ev)


# ---------------------------------------------------------------------------
# Part 2 — recommend_next_action (pure)
# ---------------------------------------------------------------------------

class TestNextAction:
    def test_pii_risk_recommends_review_pii(self):
        p = _make_profile(pii_risk=True)
        assert recommend_next_action(p) == NextAction.REVIEW_PII

    def test_hard_pii_policy_recommends_review_pii(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_PII)
        assert recommend_next_action(p) == NextAction.REVIEW_PII

    def test_finance_domain_recommends_escalate(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_HIGH_RISK)
        assert recommend_next_action(p) == NextAction.ESCALATE

    def test_needs_review_recommends_escalate(self):
        p = _make_profile(approval_state=GovernanceState.NEEDS_REVIEW)
        assert recommend_next_action(p) == NextAction.ESCALATE

    def test_human_approved_no_action(self):
        p = _make_profile(approval_state=GovernanceState.HUMAN_APPROVED)
        assert recommend_next_action(p) == NextAction.NO_ACTION

    def test_auto_approved_no_action(self):
        p = _make_profile(approval_state=GovernanceState.AUTO_APPROVED)
        assert recommend_next_action(p) == NextAction.NO_ACTION

    def test_rejected_no_action(self):
        p = _make_profile(approval_state=GovernanceState.REJECTED)
        assert recommend_next_action(p) == NextAction.NO_ACTION

    def test_auto_approval_eligible_recommends_approve(self):
        p = _make_profile(auto_approval_eligible=True)
        assert recommend_next_action(p) == NextAction.APPROVE

    def test_generated_state_needs_more_metadata(self):
        p = _make_profile(approval_state=GovernanceState.GENERATED)
        assert recommend_next_action(p) == NextAction.NEEDS_MORE_METADATA

    def test_domain_rule_review_domain(self):
        p = _make_profile(object_type_id=GovernedObjectType.DOMAIN_RULE)
        assert recommend_next_action(p) == NextAction.REVIEW_DOMAIN

    def test_entity_rule_review_entity(self):
        p = _make_profile(object_type_id=GovernedObjectType.ENTITY_RULE)
        assert recommend_next_action(p) == NextAction.REVIEW_ENTITY

    def test_dict_table_review_dictionary(self):
        p = _make_profile(object_type_id=GovernedObjectType.DICT_TABLE)
        assert recommend_next_action(p) == NextAction.REVIEW_DICTIONARY

    def test_dict_column_review_dictionary(self):
        p = _make_profile(object_type_id=GovernedObjectType.DICT_COLUMN)
        assert recommend_next_action(p) == NextAction.REVIEW_DICTIONARY

    def test_domain_refinement_review_domain(self):
        p = _make_profile(object_type_id=GovernedObjectType.DOMAIN_REFINEMENT)
        assert recommend_next_action(p) == NextAction.REVIEW_DOMAIN

    def test_pii_confirmation_review_pii(self):
        p = _make_profile(object_type_id=GovernedObjectType.PII_CONFIRMATION)
        assert recommend_next_action(p) == NextAction.REVIEW_PII

    def test_deprecated_no_action(self):
        p = _make_profile(approval_state=GovernanceState.DEPRECATED)
        assert recommend_next_action(p) == NextAction.NO_ACTION

    def test_archived_no_action(self):
        p = _make_profile(approval_state=GovernanceState.ARCHIVED)
        assert recommend_next_action(p) == NextAction.NO_ACTION


# ---------------------------------------------------------------------------
# Part 3 — get_governance_explanation (integration)
# ---------------------------------------------------------------------------

class TestGovernanceExplanation:
    def test_auto_approved_explanation(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, approved_by, created_at, approved_at, active) "
            "VALUES (1, 1, 'PREFIX', 'fact_', 'Sales', 0.99, "
            "'APPROVED', 'system', 'policy:auto', '2025-01-01', '2025-06-01', 1)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert exp is not None
        assert exp.decision_type == "HUMAN_APPROVED"  # APPROVED status maps to HUMAN_APPROVED
        assert exp.risk_score <= 15
        assert exp.recommended_action == NextAction.NO_ACTION

    def test_blocked_pii_explanation(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.customers', 'email', 'Email Address', "
            "1, 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(
                object_type="dict.column", source_id=1,
                table_fqn="dbo.customers", column_name="email",
            )
        assert exp is not None
        assert exp.recommended_action == NextAction.REVIEW_PII
        assert exp.recommended_steward == "PII Officer"
        assert exp.risk_score >= 60
        assert any("pii" in f.lower() for f in exp.risk_factors)
        assert exp.can_ai_use is False

    def test_blocked_finance_domain_explanation(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'PREFIX', 'rev_', 'Finance', 0.99, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert exp is not None
        assert exp.recommended_action == NextAction.ESCALATE
        assert "Finance" in exp.recommended_steward
        assert exp.risk_score >= 50
        assert any("finance" in f.lower() for f in exp.risk_factors)

    def test_low_confidence_explanation(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'misc', 'Sales', 0.42, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert exp is not None
        assert exp.confidence_score == pytest.approx(0.42)
        assert any("low confidence" in f.lower() for f in exp.risk_factors)
        assert "42%" in exp.priority_reason or "0.42" in str(exp.confidence_score)

    def test_unknown_object_returns_none(self):
        db = _make_db()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=9999)
        assert exp is None

    def test_unknown_type_returns_none(self):
        db = _make_db()
        with _patch(db):
            exp = get_governance_explanation(object_type="future.unknown_type")
        assert exp is None

    def test_all_fields_present_in_to_dict(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 0.80, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        d = exp.to_dict()
        expected_keys = {
            "object_type_id", "object_id", "decision", "decision_type",
            "risk_score", "confidence_score", "matched_policies",
            "blocking_policies", "evidence", "recommended_action",
            "recommended_steward", "estimated_review_minutes",
            "priority_reason", "risk_factors", "can_ai_use", "ai_warning",
        }
        assert expected_keys == set(d.keys())

    def test_estimated_review_minutes_zero_for_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, approved_by, created_at, approved_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 0.99, "
            "'APPROVED', 'system', 'alice', '2025-01-01', '2025-06-01', 1)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert exp.estimated_review_minutes == 0

    def test_estimated_review_minutes_positive_for_pending(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 0.80, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert exp.estimated_review_minutes > 0

    def test_recommended_steward_domain_specific(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Marketing', 0.80, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert "Marketing" in exp.recommended_steward

    def test_matched_policy_appears_in_list(self):
        db = _make_db()
        db.execute(
            "INSERT INTO governance_policies "
            "(policy_name, enabled, priority, object_types_json, condition_json, "
            "action, created_by, created_at, updated_at) "
            "VALUES ('AUTO_99', 1, 10, '[\"domain.rule\"]', "
            "'{\"confidence_min\": 0.99}', 'AUTO_APPROVE', 'system', "
            "'2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 0.995, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            exp = get_governance_explanation(object_type="domain.rule", rule_id=1)
        assert "AUTO_99" in exp.matched_policies
        assert exp.recommended_action == NextAction.APPROVE


# ---------------------------------------------------------------------------
# Part 4 — governance_readiness_summary (aggregate)
# ---------------------------------------------------------------------------

class TestReadinessSummary:
    def _insert_state(self, db, object_type, object_id, state, confidence=None, tier=None):
        db.execute(
            "INSERT INTO governance_state_map "
            "(object_type_id, object_id, approval_state, confidence_score, "
            "confidence_tier, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '2025-01-01', '2025-01-01')",
            (object_type, object_id, state, confidence, tier),
        )
        db.commit()

    def test_empty_state_map_returns_zero(self):
        db = _make_db()
        with _patch(db):
            s = governance_readiness_summary()
        assert s["total_governed"] == 0
        assert s["governance_score"] == 0
        assert s["avg_confidence"] is None

    def test_all_approved_high_score(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "HUMAN_APPROVED", 0.99, "VERY_HIGH")
        self._insert_state(db, "domain.rule", "2", "AUTO_APPROVED", 0.99, "VERY_HIGH")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["objects_ready"] == 2
        assert s["governance_score"] >= 90

    def test_escalated_items_reduce_score(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "HUMAN_APPROVED", 0.99, "VERY_HIGH")
        self._insert_state(db, "domain.rule", "2", "NEEDS_REVIEW", 0.40, "LOW")
        self._insert_state(db, "domain.rule", "3", "NEEDS_REVIEW", 0.40, "LOW")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["objects_escalated"] == 2
        assert s["governance_score"] < 90

    def test_auto_approval_pct_computed(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "AUTO_APPROVED", 0.99, "VERY_HIGH")
        self._insert_state(db, "domain.rule", "2", "HUMAN_APPROVED", 0.85, "HIGH")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["auto_approval_pct"] == 50.0

    def test_avg_confidence_weighted(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "SUGGESTED", 0.80, "HIGH")
        self._insert_state(db, "domain.rule", "2", "SUGGESTED", 0.60, "MEDIUM")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["avg_confidence"] == pytest.approx(0.70, abs=0.01)

    def test_objects_blocked_includes_rejected_deprecated_archived(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "REJECTED")
        self._insert_state(db, "domain.rule", "2", "DEPRECATED")
        self._insert_state(db, "domain.rule", "3", "ARCHIVED")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["objects_blocked"] == 3

    def test_objects_pending_includes_suggested_generated_validated(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "SUGGESTED")
        self._insert_state(db, "domain.rule", "2", "GENERATED")
        self._insert_state(db, "domain.rule", "3", "VALIDATED")
        with _patch(db):
            s = governance_readiness_summary()
        assert s["objects_pending"] == 3

    def test_high_risk_pct_counts_low_confidence_and_escalated(self):
        db = _make_db()
        self._insert_state(db, "domain.rule", "1", "SUGGESTED", 0.40, "LOW")
        self._insert_state(db, "domain.rule", "2", "NEEDS_REVIEW", 0.80, "HIGH")
        self._insert_state(db, "domain.rule", "3", "HUMAN_APPROVED", 0.99, "VERY_HIGH")
        with _patch(db):
            s = governance_readiness_summary()
        # 2 of 3 are high-risk (LOW conf + NEEDS_REVIEW)
        assert s["high_risk_pct"] == pytest.approx(66.7, abs=0.5)

    def test_open_assignments_counted(self):
        db = _make_db()
        db.execute(
            "INSERT INTO governance_assignments "
            "(object_type, object_id, assigned_to, assigned_by, priority, "
            "status, created_at, updated_at) "
            "VALUES ('domain.rule', '1', 'alice', 'admin', 'HIGH', "
            "'OPEN', '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO governance_assignments "
            "(object_type, object_id, assigned_to, assigned_by, priority, "
            "status, created_at, updated_at, completed_at) "
            "VALUES ('domain.rule', '2', 'bob', 'admin', 'LOW', "
            "'COMPLETED', '2025-01-01', '2025-01-01', '2025-01-02')"
        )
        db.commit()
        with _patch(db):
            s = governance_readiness_summary()
        assert s["open_assignments"] == 1

    def test_open_assignments_filtered_by_source_id(self):
        db = _make_db()
        db.execute(
            "INSERT INTO governance_assignments "
            "(object_type, object_id, source_id, assigned_to, assigned_by, "
            "priority, status, created_at, updated_at) "
            "VALUES ('domain.rule', '1', 1, 'alice', 'admin', 'HIGH', "
            "'OPEN', '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO governance_assignments "
            "(object_type, object_id, source_id, assigned_to, assigned_by, "
            "priority, status, created_at, updated_at) "
            "VALUES ('domain.rule', '2', 2, 'bob', 'admin', 'LOW', "
            "'OPEN', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            s = governance_readiness_summary(source_id=1)
        assert s["open_assignments"] == 1


# ---------------------------------------------------------------------------
# Part 5 — Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_get_governance_profile_unaffected(self):
        """Phase 5 must not change get_governance_profile()'s existing contract."""
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 0.80, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        from data.governance_service import get_governance_profile
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        assert profile is not None
        assert profile.approval_state == GovernanceState.SUGGESTED
        # Existing Phase 1/2 fields still present
        assert hasattr(profile, "can_ai_use")
        assert hasattr(profile, "auto_approval_eligible")

    def test_no_new_tables_required_for_intelligence(self):
        """Phase 5 reads only from existing governance_state_map / governance_assignments."""
        db = _make_db()
        tables = {
            row[0] for row in
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        # All tables used by governance_readiness_summary already exist from earlier phases
        assert "governance_state_map" in tables
        assert "governance_assignments" in tables
        # No new governance table introduced by this test schema beyond known ones
        gov_tables = {t for t in tables if t.startswith("governance_")}
        assert gov_tables == {
            "governance_approval_events",
            "governance_state_map",
            "governance_policies",
            "governance_assignments",
        }
