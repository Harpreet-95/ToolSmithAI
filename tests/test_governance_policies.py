"""
Tests for Phase 2 — Governance Policy Engine in data/governance_service.py.

Covers:
  - Hard safety policies (PII, high-risk domains, irreversible states)
  - DB-stored policy evaluation (AUTO_APPROVE, REQUIRE_HUMAN, ESCALATE, NO_ACTION)
  - Policy priority ordering
  - Disabled policy is ignored
  - Hard safety policies override DB policies
  - get_governance_profile() enriches profile with policy fields
  - PolicyEvaluationResult structure
  - Policy CRUD (create, list, toggle)
  - _matches_condition() logic

Run from project root:
    venv/Scripts/pytest tests/test_governance_policies.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-policies-long-enough-secret-32chars")
os.environ.setdefault("USER_ID_SALT", "test-salt-policies-phase2")

from data.governance_service import (
    GovernanceState,
    GovernedObjectType,
    GovernanceProfile,
    PolicyAction,
    PolicyEvaluationResult,
    _HARD_POLICY_PII,
    _HARD_POLICY_HIGH_RISK,
    _HARD_POLICY_IRREVERSIBLE,
    _check_hard_safety_policies,
    _check_db_policies,
    _matches_condition,
    evaluate_policies,
    get_governance_profile,
    get_governance_policies,
    create_governance_policy,
    toggle_governance_policy,
)


# ---------------------------------------------------------------------------
# Minimal in-memory schema (only tables used by governance_service)
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT NOT NULL,
        display_name TEXT NOT NULL DEFAULT 'Test Source',
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
        created_by      TEXT NOT NULL,
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
        created_by      TEXT NOT NULL,
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
        raw_type              TEXT,
        is_nullable           INTEGER NOT NULL DEFAULT 1,
        is_primary_key        INTEGER NOT NULL DEFAULT 0,
        is_identity           INTEGER NOT NULL DEFAULT 0,
        ordinal_position      INTEGER NOT NULL DEFAULT 0,
        null_count            INTEGER,
        null_percentage       REAL,
        populated_count       INTEGER,
        populated_percentage  REAL,
        empty_string_count    INTEGER,
        zero_count            INTEGER,
        distinct_count        INTEGER,
        distinct_percentage   REAL,
        uniqueness_score      REAL,
        cardinality_tier      TEXT,
        min_value             TEXT,
        max_value             TEXT,
        min_length            INTEGER,
        max_length_observed   INTEGER,
        avg_length            REAL,
        mean_value            REAL,
        std_deviation         REAL,
        p5_value              TEXT,
        p95_value             TEXT,
        dominant_pattern      TEXT,
        pattern_coverage      REAL,
        email_match_rate      REAL,
        phone_match_rate      REAL,
        guid_match_rate       REAL,
        date_string_rate      REAL,
        numeric_string_rate   REAL,
        masked_value_rate     REAL,
        semantic_type         TEXT,
        semantic_confidence   REAL,
        semantic_evidence_json TEXT,
        semantic_rule_version TEXT,
        pii_name_heuristic    INTEGER NOT NULL DEFAULT 0,
        pii_confirmed         INTEGER NOT NULL DEFAULT 0,
        pii_signals_json      TEXT,
        top_values_coverage   REAL,
        profiling_depth       TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status      TEXT NOT NULL DEFAULT 'COMPLETE',
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
    nc = _NoClose(db)
    return patch("data.governance_service.get_connection", return_value=nc)


def _insert_policy(db, *, name, action, priority=100,
                   obj_types=None, condition=None, enabled=1):
    """Helper to insert a test policy."""
    db.execute(
        """INSERT INTO governance_policies
               (policy_name, enabled, priority, object_types_json,
                condition_json, action, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'test', '2025-01-01', '2025-01-01')""",
        (name, enabled, priority,
         json.dumps(obj_types or []),
         json.dumps(condition or {}),
         action),
    )
    db.commit()


def _make_profile(**kwargs) -> GovernanceProfile:
    """Build a GovernanceProfile with test defaults; override via kwargs."""
    defaults = dict(
        object_type_id    = "domain.rule",
        object_id         = "1",
        approval_state    = GovernanceState.SUGGESTED,
        confidence_score  = 0.85,
        confidence_tier   = "HIGH",
        confidence_source = "learning_engine",
        review_required   = True,
        review_reason     = "Awaiting approval.",
        reviewed_by       = None,
        reviewed_at       = None,
        created_by        = "system",
        created_at        = "2025-01-01",
        updated_at        = "2025-01-01",
        evidence          = [],
        can_ai_use        = True,
        ai_warning        = "Awaiting review.",
        pii_risk          = False,
        domain_context    = None,
    )
    defaults.update(kwargs)
    return GovernanceProfile(**defaults)


# ---------------------------------------------------------------------------
# Part 1 — Hard Safety Policies (pure logic, no DB)
# ---------------------------------------------------------------------------

class TestHardSafetyPolicies:
    """Hard policies run first and cannot be disabled. They need no DB."""

    def test_rejected_state_is_blocked(self):
        profile = _make_profile(approval_state=GovernanceState.REJECTED)
        result = _check_hard_safety_policies(profile)
        assert result is not None
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == _HARD_POLICY_IRREVERSIBLE

    def test_deprecated_state_is_blocked(self):
        profile = _make_profile(approval_state=GovernanceState.DEPRECATED)
        result = _check_hard_safety_policies(profile)
        assert result is not None
        assert result.blocking_policy == _HARD_POLICY_IRREVERSIBLE

    def test_archived_state_is_blocked(self):
        profile = _make_profile(approval_state=GovernanceState.ARCHIVED)
        result = _check_hard_safety_policies(profile)
        assert result is not None
        assert result.blocking_policy == _HARD_POLICY_IRREVERSIBLE

    def test_pii_risk_column_blocked_regardless_of_state(self):
        profile = _make_profile(
            object_type_id = GovernedObjectType.DICT_COLUMN,
            pii_risk       = True,
            approval_state = GovernanceState.SUGGESTED,
            confidence_score = 0.99,  # even very high confidence
        )
        result = _check_hard_safety_policies(profile)
        assert result is not None
        assert result.blocking_policy == _HARD_POLICY_PII
        assert result.review_required is True

    def test_pii_confirmation_unconfirmed_blocked(self):
        profile = _make_profile(
            object_type_id = GovernedObjectType.PII_CONFIRMATION,
            approval_state = GovernanceState.SUGGESTED,
            pii_risk       = False,  # pii_risk field separate from type
        )
        result = _check_hard_safety_policies(profile)
        assert result is not None
        assert result.blocking_policy == _HARD_POLICY_PII

    def test_financial_domain_blocked(self):
        for domain in ("Finance", "Financial", "Revenue", "Billing"):
            profile = _make_profile(
                domain_context = domain,
                approval_state = GovernanceState.SUGGESTED,
                confidence_score = 0.999,
            )
            result = _check_hard_safety_policies(profile)
            assert result is not None, f"Expected Finance domain '{domain}' to be blocked"
            assert result.blocking_policy == _HARD_POLICY_HIGH_RISK
            assert result.review_required is True

    def test_regulatory_domain_blocked(self):
        for domain in ("Compliance", "Legal", "Regulatory", "HR", "Audit", "Risk"):
            profile = _make_profile(domain_context=domain)
            result = _check_hard_safety_policies(profile)
            assert result is not None, f"Expected regulatory domain '{domain}' to be blocked"
            assert result.blocking_policy == _HARD_POLICY_HIGH_RISK

    def test_normal_domain_not_blocked(self):
        profile = _make_profile(
            domain_context = "Sales",
            approval_state = GovernanceState.SUGGESTED,
        )
        result = _check_hard_safety_policies(profile)
        assert result is None  # no hard policy matched

    def test_human_approved_pii_risk_does_not_trigger_irreversible(self):
        """HUMAN_APPROVED is not in _IRREVERSIBLE_STATES."""
        profile = _make_profile(
            approval_state = GovernanceState.HUMAN_APPROVED,
            pii_risk       = False,
        )
        result = _check_hard_safety_policies(profile)
        assert result is None

    def test_approved_non_pii_no_hard_block(self):
        profile = _make_profile(
            object_type_id = GovernedObjectType.DOMAIN_RULE,
            approval_state = GovernanceState.SUGGESTED,
            pii_risk       = False,
            domain_context = "Sales",
            confidence_score = 0.99,
        )
        result = _check_hard_safety_policies(profile)
        assert result is None


# ---------------------------------------------------------------------------
# Part 2 — Condition Matching
# ---------------------------------------------------------------------------

class TestMatchesCondition:
    def _p(self, **kw) -> GovernanceProfile:
        return _make_profile(**kw)

    def test_empty_condition_always_matches(self):
        profile = self._p()
        assert _matches_condition(profile, {}) is True

    def test_confidence_min_met(self):
        profile = self._p(confidence_score=0.99)
        assert _matches_condition(profile, {"confidence_min": 0.99}) is True

    def test_confidence_min_not_met(self):
        profile = self._p(confidence_score=0.80)
        assert _matches_condition(profile, {"confidence_min": 0.99}) is False

    def test_confidence_min_missing_score(self):
        profile = self._p(confidence_score=None)
        assert _matches_condition(profile, {"confidence_min": 0.80}) is False

    def test_confidence_max_met(self):
        profile = self._p(confidence_score=0.60)
        assert _matches_condition(profile, {"confidence_max": 0.70}) is True

    def test_confidence_max_not_met(self):
        profile = self._p(confidence_score=0.90)
        assert _matches_condition(profile, {"confidence_max": 0.70}) is False

    def test_confidence_range(self):
        profile = self._p(confidence_score=0.75)
        assert _matches_condition(
            profile, {"confidence_min": 0.70, "confidence_max": 0.80}
        ) is True

    def test_domain_filter_matched(self):
        profile = self._p(domain_context="Sales")
        assert _matches_condition(profile, {"domains": ["Sales", "Marketing"]}) is True

    def test_domain_filter_not_matched(self):
        profile = self._p(domain_context="IT")
        assert _matches_condition(profile, {"domains": ["Sales"]}) is False

    def test_domain_filter_empty_matches_all(self):
        profile = self._p(domain_context="Anything")
        assert _matches_condition(profile, {"domains": []}) is True

    def test_pii_required_match(self):
        profile = self._p(pii_risk=True)
        assert _matches_condition(profile, {"pii_required": True}) is True

    def test_pii_required_no_match(self):
        profile = self._p(pii_risk=False)
        assert _matches_condition(profile, {"pii_required": True}) is False


# ---------------------------------------------------------------------------
# Part 3 — DB Policy Evaluation
# ---------------------------------------------------------------------------

class TestDbPolicies:
    def test_auto_approve_policy_returns_eligible(self):
        db = _make_db()
        _insert_policy(db, name="TEST_AUTO", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.95})
        profile = _make_profile(confidence_score=0.99)
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.auto_approval_eligible is True
        assert result.matched_policy == "TEST_AUTO"
        assert result.blocking_policy is None

    def test_require_human_policy_blocks(self):
        db = _make_db()
        _insert_policy(db, name="TEST_REQUIRE", action="REQUIRE_HUMAN")
        profile = _make_profile(confidence_score=0.99)
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == "TEST_REQUIRE"
        assert result.review_required is True

    def test_escalate_policy_blocks_auto_approve(self):
        db = _make_db()
        _insert_policy(db, name="TEST_ESCALATE", action="ESCALATE")
        profile = _make_profile()
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == "TEST_ESCALATE"
        assert result.review_required is True

    def test_no_action_policy_falls_through_to_next(self):
        db = _make_db()
        _insert_policy(db, name="NO_OP", action="NO_ACTION", priority=10)
        _insert_policy(db, name="AUTO_NEXT", action="AUTO_APPROVE",
                       priority=20, condition={"confidence_min": 0.90})
        profile = _make_profile(confidence_score=0.95)
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.matched_policy == "AUTO_NEXT"

    def test_disabled_policy_is_ignored(self):
        db = _make_db()
        _insert_policy(db, name="DISABLED", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.50}, enabled=0)
        profile = _make_profile(confidence_score=0.99)
        result = _check_db_policies(profile, db)
        assert result is None  # disabled policy not evaluated

    def test_object_type_filter_applied(self):
        db = _make_db()
        _insert_policy(db, name="DICT_ONLY", action="AUTO_APPROVE",
                       obj_types=["dict.table"], condition={})
        # Profile is domain.rule — filter excludes it
        profile = _make_profile(object_type_id="domain.rule")
        result = _check_db_policies(profile, db)
        assert result is None

    def test_object_type_empty_matches_all(self):
        db = _make_db()
        _insert_policy(db, name="ANY_TYPE", action="AUTO_APPROVE",
                       obj_types=[], condition={})
        profile = _make_profile(object_type_id="entity.rule")
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.auto_approval_eligible is True

    def test_priority_ordering_first_match_wins(self):
        db = _make_db()
        # Lower priority number (10) is evaluated first
        _insert_policy(db, name="HIGH_PRIO_REQUIRE", action="REQUIRE_HUMAN",
                       priority=10, condition={})
        _insert_policy(db, name="LOW_PRIO_AUTO", action="AUTO_APPROVE",
                       priority=50, condition={})
        profile = _make_profile(confidence_score=0.99)
        result = _check_db_policies(profile, db)
        # High-priority REQUIRE_HUMAN wins
        assert result.matched_policy == "HIGH_PRIO_REQUIRE"
        assert result.auto_approval_eligible is False

    def test_no_matching_policy_returns_none(self):
        db = _make_db()
        _insert_policy(db, name="HIGH_CONF_ONLY", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.99})
        profile = _make_profile(confidence_score=0.70)  # below threshold
        result = _check_db_policies(profile, db)
        assert result is None

    def test_confidence_min_exactly_at_threshold(self):
        db = _make_db()
        _insert_policy(db, name="AT_THRESHOLD", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.95})
        profile = _make_profile(confidence_score=0.95)
        result = _check_db_policies(profile, db)
        assert result is not None
        assert result.auto_approval_eligible is True


# ---------------------------------------------------------------------------
# Part 4 — Full evaluate_policies()
# ---------------------------------------------------------------------------

class TestEvaluatePolicies:
    def test_high_confidence_eligible_for_auto_approve(self):
        db = _make_db()
        _insert_policy(db, name="VERY_HIGH", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.99})
        profile = _make_profile(confidence_score=0.995, domain_context="Sales")
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is True
        assert result.matched_policy == "VERY_HIGH"

    def test_pii_blocks_auto_approve_despite_db_policy(self):
        """Hard PII policy wins even if a DB policy says AUTO_APPROVE."""
        db = _make_db()
        _insert_policy(db, name="AUTO_ALL", action="AUTO_APPROVE", condition={})
        profile = _make_profile(
            object_type_id = GovernedObjectType.DICT_COLUMN,
            pii_risk       = True,
            confidence_score = 0.999,
        )
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == _HARD_POLICY_PII

    def test_financial_domain_blocks_despite_high_confidence(self):
        db = _make_db()
        _insert_policy(db, name="HIGH_CONF", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.90})
        profile = _make_profile(
            confidence_score = 0.99,
            domain_context   = "Finance",
        )
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == _HARD_POLICY_HIGH_RISK

    def test_rejected_state_blocked_always(self):
        db = _make_db()
        _insert_policy(db, name="AUTO_ALL", action="AUTO_APPROVE", condition={})
        profile = _make_profile(
            approval_state = GovernanceState.REJECTED,
            confidence_score = 0.99,
        )
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy == _HARD_POLICY_IRREVERSIBLE

    def test_human_approved_skips_policy_evaluation(self):
        """HUMAN_APPROVED objects are not in _EVALUABLE_STATES → no DB query needed."""
        db = _make_db()
        _insert_policy(db, name="SOME_POLICY", action="REQUIRE_HUMAN", condition={})
        profile = _make_profile(
            approval_state = GovernanceState.HUMAN_APPROVED,
        )
        with _patch(db):
            result = evaluate_policies(profile)
        # Not in _EVALUABLE_STATES — policy evaluation skipped
        assert result.auto_approval_eligible is False
        assert result.blocking_policy is None
        assert result.review_required is False

    def test_low_confidence_not_eligible(self):
        db = _make_db()
        _insert_policy(db, name="HIGH_CONF_ONLY", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.99})
        profile = _make_profile(confidence_score=0.70)
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False
        assert result.matched_policy is None

    def test_no_policies_in_db_returns_default(self):
        db = _make_db()
        profile = _make_profile(
            approval_state = GovernanceState.SUGGESTED,
        )
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False
        assert result.blocking_policy is None
        assert result.review_required is True  # SUGGESTED → review_required

    def test_generated_state_no_review_required_by_default(self):
        db = _make_db()
        profile = _make_profile(
            approval_state = GovernanceState.GENERATED,
            review_reason  = None,
        )
        with _patch(db):
            result = evaluate_policies(profile)
        # GENERATED is in _EVALUABLE_STATES; no policy matched → default
        assert result.review_required is False

    def test_disabled_policy_ignored_in_evaluate(self):
        db = _make_db()
        _insert_policy(db, name="DISABLED_AUTO", action="AUTO_APPROVE",
                       condition={}, enabled=0)
        profile = _make_profile(confidence_score=0.99)
        with _patch(db):
            result = evaluate_policies(profile)
        assert result.auto_approval_eligible is False

    def test_policy_result_has_all_required_fields(self):
        db = _make_db()
        _insert_policy(db, name="TEST", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.90})
        profile = _make_profile(confidence_score=0.95)
        with _patch(db):
            result = evaluate_policies(profile)
        assert hasattr(result, "auto_approval_eligible")
        assert hasattr(result, "blocking_policy")
        assert hasattr(result, "matched_policy")
        assert hasattr(result, "review_required")
        assert hasattr(result, "review_reason")


# ---------------------------------------------------------------------------
# Part 5 — Profile enrichment (get_governance_profile integration)
# ---------------------------------------------------------------------------

class TestProfileEnrichment:
    def _insert_domain_rule(self, db, confidence=0.99, domain="Sales"):
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'PREFIX', 'fact_', ?, ?, 'PENDING', 'system', '2025-01-01', 0)",
            (domain, confidence),
        )
        db.commit()

    def test_profile_includes_policy_fields(self):
        db = _make_db()
        self._insert_domain_rule(db, confidence=0.80)
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.rule", rule_id=1
            )
        assert profile is not None
        assert hasattr(profile, "auto_approval_eligible")
        assert hasattr(profile, "blocking_policy")
        assert hasattr(profile, "matched_policy")

    def test_profile_to_dict_includes_policy_fields(self):
        db = _make_db()
        self._insert_domain_rule(db)
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.rule", rule_id=1
            )
        d = profile.to_dict()
        assert "auto_approval_eligible" in d
        assert "blocking_policy" in d
        assert "matched_policy" in d
        assert "pii_risk" in d
        assert "domain_context" in d

    def test_high_confidence_rule_auto_approve_eligible(self):
        db = _make_db()
        _insert_policy(db, name="AUTO_HIGH", action="AUTO_APPROVE",
                       obj_types=["domain.rule"],
                       condition={"confidence_min": 0.99})
        self._insert_domain_rule(db, confidence=0.995, domain="Sales")
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.rule", rule_id=1
            )
        assert profile.auto_approval_eligible is True
        assert profile.matched_policy == "AUTO_HIGH"

    def test_pii_column_profile_not_auto_approve_eligible(self):
        db = _make_db()
        _insert_policy(db, name="AUTO_ALL", action="AUTO_APPROVE", condition={})
        db.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.customers', 'email', 'Email Address', "
            "1, 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.column", source_id=1,
                table_fqn="dbo.customers", column_name="email"
            )
        assert profile.pii_risk is True
        assert profile.auto_approval_eligible is False
        assert profile.blocking_policy == _HARD_POLICY_PII

    def test_domain_context_populated_for_rule(self):
        db = _make_db()
        self._insert_domain_rule(db, domain="Marketing")
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.rule", rule_id=1
            )
        assert profile.domain_context == "Marketing"

    def test_domain_context_populated_for_table(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, "
            "table_type, business_name, domain, is_approved, "
            "generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.fact_sales', 'fact_sales', 'dbo', "
            "'TABLE', 'Sales Facts', 'Finance', 0, 'rule_based', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1,
                table_fqn="dbo.fact_sales"
            )
        # Finance domain should trigger hard policy
        assert profile.domain_context == "Finance"
        assert profile.blocking_policy == _HARD_POLICY_HIGH_RISK

    def test_require_human_policy_sets_review_required(self):
        db = _make_db()
        _insert_policy(db, name="DICT_REQUIRE", action="REQUIRE_HUMAN",
                       obj_types=["domain.rule"], condition={})
        self._insert_domain_rule(db, confidence=0.99, domain="Sales")
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.rule", rule_id=1
            )
        assert profile.review_required is True
        assert profile.auto_approval_eligible is False
        assert profile.blocking_policy == "DICT_REQUIRE"


# ---------------------------------------------------------------------------
# Part 6 — Policy CRUD
# ---------------------------------------------------------------------------

class TestPolicyCRUD:
    def test_create_policy_auto_approve(self):
        db = _make_db()
        with _patch(db):
            policy = create_governance_policy(
                policy_name  = "MY_AUTO_POLICY",
                action       = "AUTO_APPROVE",
                priority     = 15,
                object_types = ["domain.rule"],
                condition    = {"confidence_min": 0.97},
                created_by   = "alice",
            )
        assert policy["policy_name"] == "MY_AUTO_POLICY"
        assert policy["action"] == "AUTO_APPROVE"
        assert policy["enabled"] == 1

    def test_create_policy_invalid_action_raises(self):
        db = _make_db()
        with _patch(db):
            with pytest.raises(ValueError, match="Invalid action"):
                create_governance_policy(
                    policy_name = "BAD",
                    action      = "INVALID_ACTION",
                    created_by  = "alice",
                )

    def test_create_duplicate_policy_name_raises(self):
        db = _make_db()
        with _patch(db):
            create_governance_policy(
                policy_name = "UNIQUE", action = "NO_ACTION", created_by = "alice"
            )
            with pytest.raises(ValueError, match="already exists"):
                create_governance_policy(
                    policy_name = "UNIQUE", action = "AUTO_APPROVE", created_by = "alice"
                )

    def test_toggle_policy_disable(self):
        db = _make_db()
        _insert_policy(db, name="TOGGLEABLE", action="AUTO_APPROVE",
                       enabled=1)
        policy_id = db.execute(
            "SELECT id FROM governance_policies WHERE policy_name = 'TOGGLEABLE'"
        ).fetchone()[0]
        with _patch(db):
            result = toggle_governance_policy(
                policy_id=policy_id, enabled=False, updated_by="admin"
            )
        assert result is not None
        assert result["enabled"] == 0

    def test_toggle_policy_enable(self):
        db = _make_db()
        _insert_policy(db, name="DISABLED_ONE", action="AUTO_APPROVE", enabled=0)
        policy_id = db.execute(
            "SELECT id FROM governance_policies WHERE policy_name = 'DISABLED_ONE'"
        ).fetchone()[0]
        with _patch(db):
            result = toggle_governance_policy(
                policy_id=policy_id, enabled=True, updated_by="admin"
            )
        assert result["enabled"] == 1

    def test_toggle_nonexistent_policy_returns_none(self):
        db = _make_db()
        with _patch(db):
            result = toggle_governance_policy(
                policy_id=9999, enabled=False, updated_by="admin"
            )
        assert result is None

    def test_list_policies_returns_all(self):
        db = _make_db()
        _insert_policy(db, name="P1", action="AUTO_APPROVE", priority=10)
        _insert_policy(db, name="P2", action="REQUIRE_HUMAN", priority=20)
        with _patch(db):
            policies = get_governance_policies()
        names = [p["policy_name"] for p in policies]
        assert "P1" in names
        assert "P2" in names

    def test_list_policies_enabled_only(self):
        db = _make_db()
        _insert_policy(db, name="ACTIVE", action="AUTO_APPROVE", enabled=1)
        _insert_policy(db, name="INACTIVE", action="REQUIRE_HUMAN", enabled=0)
        with _patch(db):
            policies = get_governance_policies(enabled_only=True)
        names = [p["policy_name"] for p in policies]
        assert "ACTIVE" in names
        assert "INACTIVE" not in names

    def test_list_policies_ordered_by_priority(self):
        db = _make_db()
        _insert_policy(db, name="LAST", action="AUTO_APPROVE", priority=200)
        _insert_policy(db, name="FIRST", action="REQUIRE_HUMAN", priority=5)
        with _patch(db):
            policies = get_governance_policies()
        names = [p["policy_name"] for p in policies]
        assert names.index("FIRST") < names.index("LAST")

    def test_list_policies_includes_parsed_json(self):
        db = _make_db()
        _insert_policy(db, name="TYPED", action="AUTO_APPROVE",
                       obj_types=["domain.rule"],
                       condition={"confidence_min": 0.95})
        with _patch(db):
            policies = get_governance_policies()
        typed = next(p for p in policies if p["policy_name"] == "TYPED")
        assert typed["object_types"] == ["domain.rule"]
        assert typed["condition"]["confidence_min"] == 0.95

    def test_disabled_policy_not_evaluated_after_toggle(self):
        """End-to-end: create → toggle disabled → policy no longer applied."""
        db = _make_db()
        _insert_policy(db, name="TOGGLED", action="AUTO_APPROVE",
                       condition={"confidence_min": 0.80}, enabled=1)
        policy_id = db.execute(
            "SELECT id FROM governance_policies WHERE policy_name = 'TOGGLED'"
        ).fetchone()[0]

        profile = _make_profile(confidence_score=0.99)
        nc = _NoClose(db)
        with patch("data.governance_service.get_connection", return_value=nc):
            before = evaluate_policies(profile)
            toggle_governance_policy(
                policy_id=policy_id, enabled=False, updated_by="admin"
            )
            after = evaluate_policies(profile)

        assert before.auto_approval_eligible is True
        assert after.auto_approval_eligible is False


# ---------------------------------------------------------------------------
# Part 7 — PolicyAction enum completeness
# ---------------------------------------------------------------------------

class TestPolicyActionEnum:
    def test_all_required_actions_present(self):
        values = {a.value for a in PolicyAction}
        assert "REQUIRE_HUMAN" in values
        assert "AUTO_APPROVE"  in values
        assert "ESCALATE"      in values
        assert "NO_ACTION"     in values

    def test_string_comparison_works(self):
        assert PolicyAction.AUTO_APPROVE == "AUTO_APPROVE"
        assert PolicyAction.REQUIRE_HUMAN == "REQUIRE_HUMAN"
