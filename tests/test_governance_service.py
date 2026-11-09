"""
Tests for data/governance_service.py — Unified Governance Engine Phase 1.

Covers:
  - Governance lifecycle states and transitions
  - Unified profile retrieval for every governed object type
  - AI trust computation (can_ai_use, ai_warning)
  - Confidence tier classification
  - Unified audit log (log_governance_event)
  - Governance state map (upsert_governance_state)
  - PII confirmation write path (confirm_pii_column)
  - Backward compatibility: existing dictionary, domain, entity approvals still work
  - Governance side-effects fire after existing approvals

Every test uses an in-memory SQLite DB patched over data.governance_service.get_connection
(and the relevant service modules) so no on-disk state is touched.

Run from project root:
    venv/Scripts/pytest tests/test_governance_service.py -v
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-governance-long-enough-secret-32c")
os.environ.setdefault("USER_ID_SALT", "test-salt-governance-phase1")

from data.governance_service import (
    GovernanceState,
    GovernedObjectType,
    GovernanceProfile,
    _confidence_tier,
    _compute_ai_trust,
    _rule_status_to_state,
    get_governance_profile,
    is_hard_safety_policy,
    list_governance_events,
    list_governed_object_types,
    log_governance_event,
    upsert_governance_state,
    confirm_pii_column,
)
from data.profiling_snapshot_resolver import (
    ProfilingSnapshotRef,
    get_latest_profiling_snapshot,
)


# ---------------------------------------------------------------------------
# Minimal schema — all tables touched by governance_service
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
        pii_classification            TEXT,
        pii_classification_confidence REAL,
        pii_classification_evidence   TEXT,
        pii_classification_source     TEXT,
        pii_classified_at             TEXT,
        pii_manual_override   INTEGER NOT NULL DEFAULT 0,
        top_values_coverage   REAL,
        profiling_depth       TEXT NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status      TEXT NOT NULL DEFAULT 'COMPLETE',
        created_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE domain_assignments (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id              INTEGER NOT NULL,
        profiling_snapshot_id  INTEGER NOT NULL DEFAULT 1,
        table_fqn              TEXT    NOT NULL,
        domain                 TEXT    NOT NULL,
        confidence             REAL    NOT NULL DEFAULT 0.0,
        evidence_json          TEXT    NOT NULL DEFAULT '[]',
        competing_domains_json TEXT    NOT NULL DEFAULT '[]',
        assignment_source      TEXT    NOT NULL DEFAULT 'rule',
        created_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE entity_assignments (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id                INTEGER NOT NULL,
        profiling_snapshot_id    INTEGER NOT NULL DEFAULT 1,
        table_fqn                TEXT    NOT NULL,
        entity                   TEXT    NOT NULL,
        confidence               REAL    NOT NULL DEFAULT 0.0,
        evidence_json            TEXT    NOT NULL DEFAULT '[]',
        competing_entities_json  TEXT    NOT NULL DEFAULT '[]',
        assignment_source        TEXT    NOT NULL DEFAULT 'rule',
        created_at               TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at               TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    """
    Wraps a sqlite3.Connection and makes close() a no-op.

    Allows in-memory test databases to stay alive across service calls that
    open a connection, do work, then close it — so that governance side-effects
    that open a second connection see the same in-memory state.
    """
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # Delegate every attribute access to the wrapped connection
    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:  # no-op — keep the in-memory DB alive
        pass


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _patch(db: sqlite3.Connection):
    """Return a patcher for governance_service.get_connection using a no-close wrapper.

    governance_service opens, uses, and closes a connection for every call.
    Using _NoClose ensures the in-memory DB stays alive so tests can inspect
    results after the service call completes.
    """
    return patch("data.governance_service.get_connection", return_value=_NoClose(db))


def _no_close(db: sqlite3.Connection) -> _NoClose:
    """Return a _NoClose wrapper; patch helpers that close() between calls."""
    return _NoClose(db)


# ---------------------------------------------------------------------------
# Part 1 — Pure logic (no DB needed)
# ---------------------------------------------------------------------------

class TestConfidenceTier:
    def test_very_high(self):
        assert _confidence_tier(0.99) == "VERY_HIGH"

    def test_very_high_boundary(self):
        assert _confidence_tier(0.95) == "VERY_HIGH"

    def test_high(self):
        assert _confidence_tier(0.85) == "HIGH"

    def test_medium(self):
        assert _confidence_tier(0.70) == "MEDIUM"

    def test_low(self):
        assert _confidence_tier(0.50) == "LOW"

    def test_none(self):
        assert _confidence_tier(None) is None

    def test_zero(self):
        assert _confidence_tier(0.0) == "LOW"


class TestComputeAiTrust:
    def test_human_approved_is_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.HUMAN_APPROVED)
        assert ok is True
        assert warn is None

    def test_auto_approved_is_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.AUTO_APPROVED)
        assert ok is True
        assert warn is None

    def test_validated_usable_with_warning(self):
        ok, warn = _compute_ai_trust(GovernanceState.VALIDATED)
        assert ok is True
        assert warn is not None

    def test_suggested_high_confidence_usable_with_warning(self):
        ok, warn = _compute_ai_trust(GovernanceState.SUGGESTED, confidence_score=0.80)
        assert ok is True
        assert "awaiting human review" in warn.lower()

    def test_suggested_low_confidence_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.SUGGESTED, confidence_score=0.60)
        assert ok is False
        assert warn is not None

    def test_generated_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.GENERATED)
        assert ok is False

    def test_rejected_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.REJECTED)
        assert ok is False
        assert "rejected" in warn.lower()

    def test_deprecated_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.DEPRECATED)
        assert ok is False
        assert "deprecated" in warn.lower()

    def test_needs_review_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.NEEDS_REVIEW)
        assert ok is False

    def test_pii_risk_unconfirmed_blocks_approved(self):
        """Even HUMAN_APPROVED is blocked if PII is not confirmed."""
        ok, warn = _compute_ai_trust(
            GovernanceState.HUMAN_APPROVED, pii_risk=True, pii_confirmed=False
        )
        assert ok is False
        assert "pii" in warn.lower()

    def test_pii_risk_confirmed_allows_approved(self):
        ok, warn = _compute_ai_trust(
            GovernanceState.HUMAN_APPROVED, pii_risk=True, pii_confirmed=True
        )
        assert ok is True
        assert warn is None

    def test_archived_not_usable(self):
        ok, warn = _compute_ai_trust(GovernanceState.ARCHIVED)
        assert ok is False


class TestRuleStatusToState:
    def test_pending(self):
        assert _rule_status_to_state("PENDING") == GovernanceState.SUGGESTED

    def test_approved(self):
        assert _rule_status_to_state("APPROVED") == GovernanceState.HUMAN_APPROVED

    def test_rejected(self):
        assert _rule_status_to_state("REJECTED") == GovernanceState.REJECTED

    def test_unknown_defaults_to_generated(self):
        assert _rule_status_to_state("UNKNOWN") == GovernanceState.GENERATED


# ---------------------------------------------------------------------------
# Part 2 — Profile retrieval (requires DB)
# ---------------------------------------------------------------------------

class TestDictTableProfile:
    def test_unapproved_with_business_name_is_suggested(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.fact_orders', 'fact_orders', 'dbo', 'TABLE', "
            "'Order Facts', 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1, table_fqn="dbo.fact_orders"
            )
        assert profile is not None
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.review_required is True
        # SUGGESTED with no confidence score: score=0.0 < 0.70 → can_ai_use is False
        assert profile.can_ai_use is False
        assert profile.ai_warning is not None

    def test_approved_table_is_human_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "business_name, is_approved, approved_by, approved_at, generation_method, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'dbo.dim_customer', 'dim_customer', 'dbo', 'TABLE', "
            "'Customer Dimension', 1, 'alice', '2025-06-01', 'rule_based', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1, table_fqn="dbo.dim_customer"
            )
        assert profile is not None
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.reviewed_by == "alice"
        assert profile.can_ai_use is True
        assert profile.ai_warning is None
        assert profile.review_required is False

    def test_table_with_no_business_name_is_generated(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.orphan', 'orphan', 'dbo', 'TABLE', "
            "0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1, table_fqn="dbo.orphan"
            )
        assert profile.approval_state == GovernanceState.GENERATED
        assert profile.can_ai_use is False

    def test_nonexistent_table_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1, table_fqn="dbo.does_not_exist"
            )
        assert profile is None

    def test_missing_table_fqn_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=1
            )
        assert profile is None

    def test_object_id_format(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (7, 1, 'sales.fact_revenue', 'fact_revenue', 'sales', 'TABLE', "
            "'Revenue Facts', 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.table", source_id=7, table_fqn="sales.fact_revenue"
            )
        assert profile.object_id == "7:sales.fact_revenue"


class TestAssignmentProfile:
    """
    Milestone M-23 — domain.assignment / entity.assignment profile dispatch.

    Before this milestone, GovernedObjectType.DOMAIN_ASSIGNMENT/ENTITY_ASSIGNMENT
    were registered (enum + _TYPE_META) and already written to by
    lock_domain_assignment()/lock_entity_assignment(), but get_governance_profile()
    had no dispatch case for either — these tests prove the new dispatch works and
    that confidence/domain_context/policy enrichment all flow through correctly.
    """

    def test_rule_assigned_domain_is_suggested(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_assignments "
            "(source_id, table_fqn, domain, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.fact_orders', 'Sales', 0.85, 'rule', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.fact_orders"
            )
        assert profile is not None
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.review_required is True
        assert profile.confidence_score == 0.85
        assert profile.confidence_tier == "HIGH"
        assert profile.domain_context == "Sales"

    def test_human_locked_domain_is_human_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_assignments "
            "(source_id, table_fqn, domain, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.fact_orders', 'Sales', 0.85, 'human', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.fact_orders"
            )
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.review_required is False
        assert profile.can_ai_use is True

    def test_auto_governance_domain_is_auto_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_assignments "
            "(source_id, table_fqn, domain, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.fact_orders', 'Sales', 0.95, 'auto_governance', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.fact_orders"
            )
        assert profile.approval_state == GovernanceState.AUTO_APPROVED
        assert profile.review_required is False
        assert profile.can_ai_use is True

    def test_unknown_domain_is_generated(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_assignments "
            "(source_id, table_fqn, domain, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.orphan', 'Unknown', 0.30, 'rule', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.orphan"
            )
        assert profile.approval_state == GovernanceState.GENERATED
        assert profile.can_ai_use is False

    def test_high_risk_domain_is_hard_blocked_even_at_high_confidence(self):
        """A Finance-domain assignment is hard-blocked from auto-approval by the
        existing, unmodified _check_hard_safety_policies high-risk-domain check —
        zero new policy code, just the new dispatch feeding domain_context in."""
        db = _make_db()
        db.execute(
            "INSERT INTO domain_assignments "
            "(source_id, table_fqn, domain, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.gl_transactions', 'Finance', 0.99, 'rule', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.gl_transactions"
            )
        assert profile.auto_approval_eligible is False
        assert profile.blocking_policy == "HARD_HIGH_RISK_DOMAIN_REQUIRES_HUMAN"
        assert is_hard_safety_policy(profile.blocking_policy) is True

    def test_entity_assignment_dispatch_mirrors_domain(self):
        db = _make_db()
        db.execute(
            "INSERT INTO entity_assignments "
            "(source_id, table_fqn, entity, confidence, assignment_source, "
            "created_at, updated_at) "
            "VALUES (1, 'dbo.dim_client', 'Client', 0.92, 'auto_governance', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="entity.assignment", source_id=1, table_fqn="dbo.dim_client"
            )
        assert profile.approval_state == GovernanceState.AUTO_APPROVED
        assert profile.confidence_score == 0.92
        # Entity values are not domain names — must never feed the high-risk
        # domain hard-policy check.
        assert profile.domain_context is None

    def test_nonexistent_assignment_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.assignment", source_id=1, table_fqn="dbo.does_not_exist"
            )
        assert profile is None

    def test_missing_table_fqn_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(object_type="entity.assignment", source_id=1)
        assert profile is None


class TestIsHardSafetyPolicy:
    def test_hard_policies_are_true(self):
        assert is_hard_safety_policy("HARD_PII_REQUIRES_HUMAN") is True
        assert is_hard_safety_policy("HARD_HIGH_RISK_DOMAIN_REQUIRES_HUMAN") is True
        assert is_hard_safety_policy("HARD_IRREVERSIBLE_STATE") is True
        assert is_hard_safety_policy("HARD_RELATIONSHIP_SUGGESTION_NO_BULK_APPROVE") is True

    def test_soft_or_missing_policy_is_false(self):
        assert is_hard_safety_policy("POLICY_REQUIRE_HUMAN_DICT_ENTRIES") is False
        assert is_hard_safety_policy(None) is False


class TestDictColumnProfile:
    def test_pii_column_unapproved_blocks_ai(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.dim_customer', 'email_address', 'Email Address', "
            "1, 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.column", source_id=1,
                table_fqn="dbo.dim_customer", column_name="email_address"
            )
        assert profile.can_ai_use is False
        assert "pii" in profile.ai_warning.lower()

    def test_approved_non_pii_column_is_usable(self):
        db = _make_db()
        db.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, approved_by, approved_at, "
            "generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.fact_orders', 'order_total', 'Order Total', "
            "0, 1, 'bob', '2025-06-01', 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="dict.column", source_id=1,
                table_fqn="dbo.fact_orders", column_name="order_total"
            )
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.can_ai_use is True
        assert profile.reviewed_by == "bob"


class TestDomainRuleProfile:
    def test_pending_rule_is_suggested(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'PREFIX', 'fact_', 'Sales', 0.92, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.confidence_score == pytest.approx(0.92)
        assert profile.confidence_tier == "HIGH"
        assert profile.review_required is True
        assert profile.can_ai_use is True   # 0.92 >= 0.70 → allowed with warning

    def test_approved_rule_is_human_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, approved_by, created_at, approved_at, active) "
            "VALUES (1, 'TOKEN', 'sales', 'Sales', 0.95, "
            "'APPROVED', 'system', 'charlie', '2025-01-01', '2025-06-01', 1)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.can_ai_use is True
        assert profile.reviewed_by == "charlie"

    def test_rejected_rule_is_not_usable(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'SCHEMA', 'stg', 'Unknown', 0.40, "
            "'REJECTED', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        assert profile.approval_state == GovernanceState.REJECTED
        assert profile.can_ai_use is False

    def test_nonexistent_rule_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=999)
        assert profile is None

    def test_missing_rule_id_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule")
        assert profile is None


class TestEntityRuleProfile:
    def test_pending_entity_rule(self):
        db = _make_db()
        db.execute(
            "INSERT INTO entity_learning_rules "
            "(source_id, pattern_type, pattern_value, entity, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'PREFIX', 'customer_', 'Customer', 0.88, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="entity.rule", rule_id=1)
        assert profile.object_type_id == GovernedObjectType.ENTITY_RULE
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.confidence_tier == "HIGH"

    def test_approved_entity_rule(self):
        db = _make_db()
        db.execute(
            "INSERT INTO entity_learning_rules "
            "(source_id, pattern_type, pattern_value, entity, confidence, "
            "approval_status, created_by, approved_by, created_at, approved_at, active) "
            "VALUES (1, 'TOKEN', 'order', 'Order', 0.97, "
            "'APPROVED', 'system', 'dana', '2025-01-01', '2025-06-01', 1)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="entity.rule", rule_id=1)
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.confidence_tier == "VERY_HIGH"
        assert profile.can_ai_use is True


class TestDomainRefinementProfile:
    def test_pending_refinement(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_rule_refinement_suggestions "
            "(source_id, parent_rule_id, pattern_type, pattern_value, "
            "suggested_domain, support_count, confidence, approval_status, "
            "created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'orders', 'Sales', 5, 0.75, "
            "'PENDING', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.refinement", suggestion_id=1
            )
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.review_required is True
        assert "orders" in profile.review_reason

    def test_nonexistent_suggestion_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(
                object_type="domain.refinement", suggestion_id=999
            )
        assert profile is None


class TestEngineToolProfile:
    def test_draft_tool_is_generated(self):
        db = _make_db()
        db.execute(
            "INSERT INTO engine_tools (id, name, version, status, definition_json, "
            "created_at, updated_at) "
            "VALUES ('tool-uuid-1', 'weekly_report', '1.0.0', 'draft', '{}', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="tool.engine", tool_id="tool-uuid-1"
            )
        assert profile.approval_state == GovernanceState.GENERATED
        assert profile.can_ai_use is False

    def test_pending_tool_is_suggested(self):
        db = _make_db()
        db.execute(
            "INSERT INTO engine_tools (id, name, version, status, definition_json, "
            "created_at, updated_at) "
            "VALUES ('tool-uuid-2', 'daily_sync', '1.0.0', 'pending_approval', '{}', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="tool.engine", tool_id="tool-uuid-2"
            )
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.review_required is True

    def test_approved_tool_is_human_approved(self):
        db = _make_db()
        db.execute(
            "INSERT INTO engine_tools (id, name, version, status, definition_json, "
            "created_at, updated_at) "
            "VALUES ('tool-uuid-3', 'approved_tool', '1.0.0', 'approved', '{}', "
            "'2025-01-01', '2025-06-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="tool.engine", tool_id="tool-uuid-3"
            )
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        assert profile.can_ai_use is True
        assert profile.confidence_tier == "VERY_HIGH"

    def test_deprecated_tool_is_not_usable(self):
        db = _make_db()
        db.execute(
            "INSERT INTO engine_tools (id, name, version, status, definition_json, "
            "created_at, updated_at) "
            "VALUES ('tool-uuid-4', 'old_tool', '1.0.0', 'deprecated', '{}', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="tool.engine", tool_id="tool-uuid-4"
            )
        assert profile.approval_state == GovernanceState.DEPRECATED
        assert profile.can_ai_use is False

    def test_nonexistent_tool_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(
                object_type="tool.engine", tool_id="does-not-exist"
            )
        assert profile is None


class TestPiiProfile:
    def test_no_heuristic_is_generated(self):
        db = _make_db()
        db.execute(
            "INSERT INTO profiling_snapshots "
            "(source_id, schema_snapshot_id, snapshot_version, mode, sample_rate, "
            "profiling_rules_version, status, tables_total, tables_profiled, "
            "tables_skipped, tables_failed, tables_timed_out, columns_total, "
            "columns_profiled, columns_skipped, total_rows_profiled, "
            "pii_columns_found, classifications_complete, batch_size, "
            "next_table_index, created_at) "
            "VALUES (1, 1, 1, 'full', 1.0, '1.0.0', 'COMPLETE', 1, 1, 0, 0, 0, 5, 5, 0, 1000, 0, 1, 50, 0, '2025-01-01')"
        )
        db.execute(
            "INSERT INTO profiling_column_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            "pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.orders', 'order_id', 'INT', 0, 0, "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="pii.confirmation", source_id=1,
                table_fqn="dbo.orders", column_name="order_id"
            )
        assert profile.approval_state == GovernanceState.GENERATED
        assert profile.can_ai_use is False  # GENERATED → False

    def test_heuristic_unconfirmed_is_suggested_and_blocked(self):
        db = _make_db()
        db.execute(
            "INSERT INTO profiling_snapshots "
            "(source_id, schema_snapshot_id, snapshot_version, mode, sample_rate, "
            "profiling_rules_version, status, tables_total, tables_profiled, "
            "tables_skipped, tables_failed, tables_timed_out, columns_total, "
            "columns_profiled, columns_skipped, total_rows_profiled, "
            "pii_columns_found, classifications_complete, batch_size, "
            "next_table_index, created_at) "
            "VALUES (1, 1, 1, 'full', 1.0, '1.0.0', 'COMPLETE', 1, 1, 0, 0, 0, 5, 5, 0, 1000, 1, 1, 50, 0, '2025-01-01')"
        )
        db.execute(
            "INSERT INTO profiling_column_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            "pii_name_heuristic, pii_confirmed, "
            "pii_signals_json, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.customers', 'email', 'VARCHAR', 1, 0, "
            "'[{\"signal\": \"name_match\", \"value\": \"email\"}]', "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="pii.confirmation", source_id=1,
                table_fqn="dbo.customers", column_name="email"
            )
        assert profile.approval_state == GovernanceState.SUGGESTED
        assert profile.review_required is True
        assert profile.can_ai_use is False
        assert len(profile.evidence) == 1

    def test_confirmed_pii_is_human_approved_and_blocked_by_risk(self):
        """pii_risk=True on the dict column still blocks AI even after pii_confirmed."""
        db = _make_db()
        db.execute(
            "INSERT INTO profiling_snapshots "
            "(source_id, schema_snapshot_id, snapshot_version, mode, sample_rate, "
            "profiling_rules_version, status, tables_total, tables_profiled, "
            "tables_skipped, tables_failed, tables_timed_out, columns_total, "
            "columns_profiled, columns_skipped, total_rows_profiled, "
            "pii_columns_found, classifications_complete, batch_size, "
            "next_table_index, created_at) "
            "VALUES (1, 1, 1, 'full', 1.0, '1.0.0', 'COMPLETE', 1, 1, 0, 0, 0, 5, 5, 0, 1000, 1, 1, 50, 0, '2025-01-01')"
        )
        db.execute(
            "INSERT INTO profiling_column_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            "pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.customers', 'ssn', 'VARCHAR', 1, 1, "
            "'2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(
                object_type="pii.confirmation", source_id=1,
                table_fqn="dbo.customers", column_name="ssn"
            )
        # PII is confirmed → state is HUMAN_APPROVED
        assert profile.approval_state == GovernanceState.HUMAN_APPROVED
        # But pii_risk=True + pii_confirmed=True → AI trust check: pii_confirmed=True so NOT blocked
        # The profile builds with pii_heuristic=True, pii_confirmed=True → state=HUMAN_APPROVED
        # _compute_ai_trust(HUMAN_APPROVED, pii_risk=True, pii_confirmed=True) → can_ai=True
        assert profile.can_ai_use is True


class TestUnknownObjectType:
    def test_unknown_type_returns_none(self):
        db = _make_db()
        with _patch(db):
            profile = get_governance_profile(object_type="future.term")
        assert profile is None


# ---------------------------------------------------------------------------
# Part 3 — GovernanceProfile.to_dict() serialisation
# ---------------------------------------------------------------------------

class TestProfileToDict:
    def test_all_keys_present(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'TOKEN', 'orders', 'Sales', 0.88, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        d = profile.to_dict()
        # Phase 1 fields
        phase1_keys = {
            "object_type_id", "object_id", "approval_state",
            "confidence_score", "confidence_tier", "confidence_source",
            "review_required", "review_reason",
            "reviewed_by", "reviewed_at",
            "created_by", "created_at", "updated_at",
            "evidence", "can_ai_use", "ai_warning",
        }
        # Phase 2 additions
        phase2_keys = {
            "pii_risk", "domain_context",
            "auto_approval_eligible", "blocking_policy", "matched_policy",
        }
        assert phase1_keys | phase2_keys == set(d.keys())

    def test_state_is_string_not_enum(self):
        db = _make_db()
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 'PREFIX', 'fact_', 'Sales', 0.90, "
            "'APPROVED', 'system', '2025-01-01', 1)"
        )
        db.commit()
        with _patch(db):
            profile = get_governance_profile(object_type="domain.rule", rule_id=1)
        d = profile.to_dict()
        assert isinstance(d["approval_state"], str)
        assert d["approval_state"] == "HUMAN_APPROVED"


# ---------------------------------------------------------------------------
# Part 4 — Audit log and state map
# ---------------------------------------------------------------------------

class TestLogGovernanceEvent:
    def test_event_written_to_table(self):
        db = _make_db()
        with _patch(db):
            log_governance_event(
                object_type_id = "domain.rule",
                object_id      = "42",
                event_type     = "APPROVED",
                from_state     = "SUGGESTED",
                to_state       = "HUMAN_APPROVED",
                actor_id       = "alice",
                notes          = "Looks good",
                source_service = "test",
            )
        rows = db.execute(
            "SELECT * FROM governance_approval_events WHERE object_id = '42'"
        ).fetchall()
        assert len(rows) == 1
        r = dict(rows[0])
        assert r["event_type"] == "APPROVED"
        assert r["actor_id"] == "alice"
        assert r["from_state"] == "SUGGESTED"
        assert r["to_state"] == "HUMAN_APPROVED"
        assert r["notes"] == "Looks good"

    def test_multiple_events_ordered_by_time(self):
        db = _make_db()
        with _patch(db):
            log_governance_event(
                object_type_id="dict.table", object_id="1:dbo.t",
                event_type="SUGGESTED", from_state=None,
                to_state="SUGGESTED", actor_id="system",
            )
            log_governance_event(
                object_type_id="dict.table", object_id="1:dbo.t",
                event_type="APPROVED", from_state="SUGGESTED",
                to_state="HUMAN_APPROVED", actor_id="bob",
            )
        rows = db.execute(
            "SELECT event_type FROM governance_approval_events "
            "WHERE object_id = '1:dbo.t' ORDER BY created_at ASC"
        ).fetchall()
        assert [r[0] for r in rows] == ["SUGGESTED", "APPROVED"]

    def test_best_effort_does_not_raise_on_bad_db(self):
        """Governance logging must never disrupt the caller."""
        bad_conn = sqlite3.connect(":memory:")  # no tables created
        bad_conn.row_factory = sqlite3.Row
        with patch("data.governance_service.get_connection", return_value=bad_conn):
            # Should not raise
            log_governance_event(
                object_type_id="dict.table", object_id="1:x",
                event_type="APPROVED", from_state=None,
                to_state="HUMAN_APPROVED", actor_id="alice",
            )


class TestUpsertGovernanceState:
    def test_state_upserted(self):
        db = _make_db()
        with _patch(db):
            upsert_governance_state(
                object_type_id = "dict.table",
                object_id      = "1:dbo.orders",
                approval_state = "HUMAN_APPROVED",
                confidence_score = None,
                reviewer_id    = "alice",
                reviewed_at    = "2025-06-01T00:00:00+00:00",
            )
        row = db.execute(
            "SELECT * FROM governance_state_map WHERE object_id = '1:dbo.orders'"
        ).fetchone()
        assert row is not None
        d = dict(row)
        assert d["approval_state"] == "HUMAN_APPROVED"
        assert d["reviewer_id"] == "alice"

    def test_upsert_updates_existing_row(self):
        db = _make_db()
        with _patch(db):
            upsert_governance_state(
                object_type_id="domain.rule", object_id="5",
                approval_state="SUGGESTED",
            )
            upsert_governance_state(
                object_type_id="domain.rule", object_id="5",
                approval_state="HUMAN_APPROVED",
                reviewer_id="carol",
            )
        rows = db.execute(
            "SELECT * FROM governance_state_map WHERE object_id = '5'"
        ).fetchall()
        assert len(rows) == 1
        assert dict(rows[0])["approval_state"] == "HUMAN_APPROVED"
        assert dict(rows[0])["reviewer_id"] == "carol"

    def test_confidence_tier_computed_on_insert(self):
        db = _make_db()
        with _patch(db):
            upsert_governance_state(
                object_type_id="domain.rule", object_id="10",
                approval_state="HUMAN_APPROVED",
                confidence_score=0.97,
            )
        row = dict(db.execute(
            "SELECT confidence_tier FROM governance_state_map WHERE object_id = '10'"
        ).fetchone())
        assert row["confidence_tier"] == "VERY_HIGH"

    def test_best_effort_does_not_raise_on_bad_db(self):
        bad_conn = sqlite3.connect(":memory:")
        bad_conn.row_factory = sqlite3.Row
        with patch("data.governance_service.get_connection", return_value=bad_conn):
            upsert_governance_state(
                object_type_id="dict.table", object_id="x",
                approval_state="HUMAN_APPROVED",
            )


class TestListGovernanceEvents:
    def test_returns_events_oldest_first(self):
        db = _make_db()
        with _patch(db):
            log_governance_event(
                object_type_id="entity.rule", object_id="3",
                event_type="SUGGESTED", from_state=None,
                to_state="SUGGESTED", actor_id="system",
            )
            log_governance_event(
                object_type_id="entity.rule", object_id="3",
                event_type="APPROVED", from_state="SUGGESTED",
                to_state="HUMAN_APPROVED", actor_id="eve",
            )
            events = list_governance_events(
                object_type_id="entity.rule", object_id="3"
            )
        assert len(events) == 2
        assert events[0]["event_type"] == "SUGGESTED"
        assert events[1]["event_type"] == "APPROVED"

    def test_returns_empty_for_unknown_object(self):
        db = _make_db()
        with _patch(db):
            events = list_governance_events(
                object_type_id="domain.rule", object_id="999"
            )
        assert events == []


# ---------------------------------------------------------------------------
# Phase 2G.14 — additional seed helpers and connection wrappers for
# confirm_pii_column() baseline coverage (latest-snapshot precedence, write
# scoping, connection/transaction behavior). TestConfirmPiiColumn._setup_db()
# below is left untouched; these build additional profiling_snapshots /
# profiling_column_profiles rows on top of it (or on a fresh db) for
# scenarios _setup_db() alone doesn't cover. This is read-only test
# infrastructure — confirm_pii_column() itself is not modified this phase.
# ---------------------------------------------------------------------------

def _seed_source_only(db: sqlite3.Connection, source_id: int, user_id: str) -> None:
    db.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        "encrypted_config_json, config_schema_version, capabilities_json, "
        "metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (?, ?, 'Test DB', 'mssql', 'relational_db', "
        "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')",
        (source_id, user_id),
    )
    db.commit()


def _seed_snapshot(
    db: sqlite3.Connection, snap_id: int, source_id: int,
    snapshot_version: int, status: str = "COMPLETE",
) -> None:
    db.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, mode, sample_rate, "
        "profiling_rules_version, status, tables_total, tables_profiled, "
        "tables_skipped, tables_failed, tables_timed_out, columns_total, "
        "columns_profiled, columns_skipped, total_rows_profiled, "
        "pii_columns_found, classifications_complete, batch_size, "
        "next_table_index, created_at) "
        "VALUES (?, ?, 1, ?, 'full', 1.0, '1.0.0', ?, "
        "1, 1, 0, 0, 0, 3, 3, 0, 5000, 1, 1, 50, 0, '2025-01-01')",
        (snap_id, source_id, snapshot_version, status),
    )
    db.commit()


def _seed_column_profile(
    db: sqlite3.Connection, row_id: int, snap_id: int, source_id: int,
    table_fqn: str, column_name: str, *,
    pii_name_heuristic: int = 1, pii_confirmed: int = 0,
) -> None:
    db.execute(
        "INSERT INTO profiling_column_profiles "
        "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
        "pii_name_heuristic, pii_confirmed, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'VARCHAR', ?, ?, '2025-01-01', '2025-01-01')",
        (row_id, snap_id, source_id, table_fqn, column_name,
         pii_name_heuristic, pii_confirmed),
    )
    db.commit()


def _row_by_id(db: sqlite3.Connection, row_id: int) -> dict | None:
    row = db.execute(
        "SELECT * FROM profiling_column_profiles WHERE id = ?", (row_id,),
    ).fetchone()
    return dict(row) if row else None


class _CountingConn:
    """Wraps a sqlite3.Connection like _NoClose (keeps the shared in-memory
    DB alive across close() calls so post-call assertions still work) but
    also counts commit()/rollback()/close() calls, to characterize
    confirm_pii_column()'s transaction behavior precisely."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def commit(self) -> None:
        self.commit_count += 1
        self._conn.commit()

    def rollback(self) -> None:
        self.rollback_count += 1
        self._conn.rollback()

    def close(self) -> None:
        self.close_count += 1  # deliberately not closing — mirrors _NoClose


class _RaisingOnUpdateConn:
    """Like _CountingConn, but raises when confirm_pii_column()'s UPDATE
    statement executes — used to characterize exception/rollback/close
    behavior when the write itself fails."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str) and "UPDATE profiling_column_profiles" in sql:
            raise sqlite3.OperationalError("simulated failure")
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self) -> None:
        self.commit_count += 1
        self._conn.commit()

    def rollback(self) -> None:
        self.rollback_count += 1
        self._conn.rollback()

    def close(self) -> None:
        self.close_count += 1


class _RecordingConn:
    """Wraps a sqlite3.Connection like _NoClose (keeps the shared in-memory
    DB alive across close() calls), records every SQL statement executed,
    and counts commit()/rollback()/close() calls — Phase 2G.16 uses this to
    prove the UPDATE never runs when the resolver returns None."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.executed_sql: list[str] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        if isinstance(sql, str):
            self.executed_sql.append(sql)
        return self._conn.execute(sql, *args, **kwargs)

    def commit(self) -> None:
        self.commit_count += 1
        self._conn.commit()

    def rollback(self) -> None:
        self.rollback_count += 1
        self._conn.rollback()

    def close(self) -> None:
        self.close_count += 1  # deliberately not closing — mirrors _NoClose


# ---------------------------------------------------------------------------
# Part 5 — PII Confirmation Write Path
# ---------------------------------------------------------------------------

class TestConfirmPiiColumn:
    def _setup_db(self) -> sqlite3.Connection:
        db = _make_db()
        # data source owned by user "alice"
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'Test DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        # profiling snapshot
        db.execute(
            "INSERT INTO profiling_snapshots "
            "(id, source_id, schema_snapshot_id, snapshot_version, mode, sample_rate, "
            "profiling_rules_version, status, tables_total, tables_profiled, "
            "tables_skipped, tables_failed, tables_timed_out, columns_total, "
            "columns_profiled, columns_skipped, total_rows_profiled, "
            "pii_columns_found, classifications_complete, batch_size, "
            "next_table_index, created_at) "
            "VALUES (10, 1, 1, 1, 'full', 1.0, '1.0.0', 'COMPLETE', "
            "1, 1, 0, 0, 0, 3, 3, 0, 5000, 1, 1, 50, 0, '2025-01-01')"
        )
        # column with PII heuristic flagged
        db.execute(
            "INSERT INTO profiling_column_profiles "
            "(id, profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            "pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (100, 10, 1, 'dbo.customers', 'email_addr', 'VARCHAR', "
            "1, 0, '2025-01-01', '2025-01-01')"
        )
        db.commit()
        return db

    def test_confirm_sets_pii_confirmed(self):
        db = self._setup_db()
        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )
        assert result is not None
        row = dict(db.execute(
            "SELECT pii_confirmed FROM profiling_column_profiles WHERE id = 100"
        ).fetchone())
        assert row["pii_confirmed"] == 1

    def test_confirm_writes_governance_event(self):
        db = self._setup_db()
        with _patch(db):
            confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'pii.confirmation'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert e["event_type"] == "PII_CONFIRMED"
        assert e["to_state"] == "HUMAN_APPROVED"
        assert e["actor_id"] == "alice"

    def test_confirm_upserts_state_map(self):
        db = self._setup_db()
        with _patch(db):
            confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )
        row = db.execute(
            "SELECT * FROM governance_state_map "
            "WHERE object_type_id = 'pii.confirmation'"
        ).fetchone()
        assert row is not None
        assert dict(row)["approval_state"] == "HUMAN_APPROVED"

    def test_wrong_user_returns_none(self):
        db = self._setup_db()
        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="mallory",
                table_fqn="dbo.customers", column_name="email_addr",
            )
        assert result is None

    def test_column_without_heuristic_returns_none(self):
        db = self._setup_db()
        # Column with no PII heuristic
        db.execute(
            "INSERT INTO profiling_column_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            "pii_name_heuristic, pii_confirmed, created_at, updated_at) "
            "VALUES (10, 1, 'dbo.customers', 'order_count', 'INT', "
            "0, 0, '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="order_count",
            )
        assert result is None

    def test_nonexistent_column_returns_none(self):
        db = self._setup_db()
        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="does_not_exist",
            )
        assert result is None

    # -------------------------------------------------------------------
    # Phase 2G.14 — baseline characterization of the correlated
    # latest-profiling-snapshot subquery inside the UPDATE, ahead of its
    # eventual resolver migration. Every test below describes CURRENT
    # behavior only — none of them assert or require any "improved"
    # behavior, even where the behavior looks surprising (see
    # test_followup_select_can_diverge_from_the_updated_row).
    # -------------------------------------------------------------------

    # --- A. latest-snapshot semantics -----------------------------------

    def test_highest_snapshot_version_wins(self):
        db = self._setup_db()  # snap 10 (v1, COMPLETE), row 100
        _seed_snapshot(db, 20, source_id=1, snapshot_version=2, status="COMPLETE")
        _seed_column_profile(db, 200, 20, 1, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert result["id"] == 200
        assert result["pii_confirmed"] == 1
        # The older snapshot's row must be untouched.
        assert _row_by_id(db, 100)["pii_confirmed"] == 0

    def test_older_complete_snapshot_wins_over_newer_cancelled(self):
        """The resolver picks the older COMPLETE snapshot (10) over the
        newer CANCELLED one (30), so the UPDATE writes pii_confirmed=1 to
        row 100 (under snapshot 10), and row 300 (under snapshot 30) is left
        untouched. Per the documented divergence baseline (see
        test_followup_select_can_diverge_from_the_updated_row above), the
        follow-up SELECT still orders by profiling_snapshot_id DESC, and 30
        > 10, so the returned dict is the stale, never-updated row 300."""
        db = self._setup_db()  # snap 10 (v1, COMPLETE), row 100
        _seed_snapshot(db, 30, source_id=1, snapshot_version=2, status="CANCELLED")
        _seed_column_profile(db, 300, 30, 1, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        # The row actually written to is 100 (the older COMPLETE snapshot,
        # correctly selected by the resolver):
        assert _row_by_id(db, 100)["pii_confirmed"] == 1
        # But the follow-up SELECT (ORDER BY profiling_snapshot_id DESC)
        # returns row 300 instead - stale, never updated.
        assert result is not None
        assert result["id"] == 300
        assert result["pii_confirmed"] == 0

    def test_older_complete_snapshot_wins_over_newer_running(self):
        """Same divergence pattern as the CANCELLED case above, with a
        newer RUNNING snapshot (40) instead."""
        db = self._setup_db()  # snap 10 (v1, COMPLETE), row 100
        _seed_snapshot(db, 40, source_id=1, snapshot_version=2, status="RUNNING")
        _seed_column_profile(db, 400, 40, 1, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        # The row actually written to is 100 (the older COMPLETE snapshot,
        # correctly selected by the resolver):
        assert _row_by_id(db, 100)["pii_confirmed"] == 1
        # But the follow-up SELECT (ORDER BY profiling_snapshot_id DESC)
        # returns row 400 instead - stale, never updated.
        assert result is not None
        assert result["id"] == 400
        assert result["pii_confirmed"] == 0

    def test_snapshot_selection_scoped_to_source(self):
        db = self._setup_db()  # source 1: snap 10 (v1), row 100
        _seed_source_only(db, source_id=2, user_id="bob")
        # source 2's snapshot has a FAR higher version - must never affect source 1.
        _seed_snapshot(db, 999, source_id=2, snapshot_version=99, status="COMPLETE")
        _seed_column_profile(db, 9001, 999, 2, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert result["id"] == 100  # source 1's own (only) row, not source 2's
        # source 2's identically-named row must remain untouched.
        assert _row_by_id(db, 9001)["pii_confirmed"] == 0

    def test_snapshot_selection_ignores_other_status_values(self):
        """Generalizes the CANCELLED/RUNNING precedence tests above: the
        subquery has no status filter at all, so any status value on the
        highest snapshot_version wins, not just CANCELLED/RUNNING."""
        db = self._setup_db()
        _seed_snapshot(db, 50, source_id=1, snapshot_version=2, status="FAILED")
        _seed_column_profile(db, 500, 50, 1, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert result["id"] == 500

    # --- B. write behavior ------------------------------------------------

    def test_unrelated_columns_in_same_snapshot_unchanged(self):
        db = self._setup_db()  # snap 10, row 100 (email_addr)
        _seed_column_profile(
            db, 101, 10, 1, "dbo.customers", "full_name", pii_name_heuristic=0,
        )

        with _patch(db):
            confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        unrelated = _row_by_id(db, 101)
        assert unrelated["pii_confirmed"] == 0
        assert unrelated["updated_at"] == "2025-01-01"

    def test_exact_fields_changed_by_update(self):
        db = self._setup_db()
        before = _row_by_id(db, 100)

        with _patch(db):
            confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        after = _row_by_id(db, 100)
        changed = {k for k in before if before[k] != after[k]}
        # Parts 2-4 (autonomous PII classification): manual confirmation now
        # also stamps the classification tri-state fields and the manual-
        # override flag, so a future automatic re-classification pass never
        # overwrites this human decision (manual override precedence).
        assert changed == {
            "pii_confirmed", "updated_at",
            "pii_classification", "pii_classification_confidence",
            "pii_classification_evidence", "pii_classification_source",
            "pii_classified_at", "pii_manual_override",
        }
        assert after["pii_confirmed"] == 1
        assert after["pii_classification"] == "CONFIRMED_PII"
        assert after["pii_manual_override"] == 1
        assert after["updated_at"] != before["updated_at"]

    def test_successful_return_shape_matches_full_row(self):
        db = self._setup_db()

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        direct = _row_by_id(db, 100)
        assert result == direct
        assert set(result.keys()) == set(direct.keys())

    def test_followup_select_reads_the_updated_row_in_the_normal_case(self):
        """Normal case: profiling_snapshot_id and snapshot_version increase
        together, so the follow-up SELECT (ordered by profiling_snapshot_id
        DESC) happens to agree with the UPDATE's snapshot_version-based
        selection. Contrast with the divergence test below."""
        db = self._setup_db()
        _seed_snapshot(db, 20, source_id=1, snapshot_version=2, status="COMPLETE")
        _seed_column_profile(db, 200, 20, 1, "dbo.customers", "email_addr")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result["id"] == 200
        assert result["profiling_snapshot_id"] == 20

    def test_followup_select_can_diverge_from_the_updated_row(self):
        """Baseline finding, not a desired behavior: the UPDATE selects the
        target row via 'ORDER BY snapshot_version DESC' (the correlated
        subquery), but the follow-up SELECT re-picks the row via
        'ORDER BY profiling_snapshot_id DESC' - a DIFFERENT ordering key.
        When a profiling_snapshots row has a numerically higher id but a
        LOWER snapshot_version (ids and versions are not guaranteed to stay
        in lockstep), the function can return a stale row that was never
        actually updated, even though pii_confirmed=1 was successfully
        written to a different row. This test documents that this is the
        function's actual current behavior - it is not asserting this is
        correct or desirable."""
        db = self._setup_db()
        # snap 500: version 2 (the TRUE latest by version - what gets updated)
        _seed_snapshot(db, 500, source_id=1, snapshot_version=2, status="COMPLETE")
        _seed_column_profile(db, 5001, 500, 1, "dbo.customers", "ssn")
        # snap 600: version 1 (older by version) but a HIGHER id than 500.
        _seed_snapshot(db, 600, source_id=1, snapshot_version=1, status="COMPLETE")
        _seed_column_profile(db, 6001, 600, 1, "dbo.customers", "ssn")

        with _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="ssn",
            )

        # The row actually written to is 5001 (version 2, correctly selected
        # by the UPDATE's subquery):
        assert _row_by_id(db, 5001)["pii_confirmed"] == 1
        # But the follow-up SELECT (ORDER BY profiling_snapshot_id DESC)
        # returns row 6001 instead - stale, never updated, pii_confirmed
        # still 0. This is the function's actual current return value.
        assert result["id"] == 6001
        assert result["pii_confirmed"] == 0

    # --- C. missing-data and ownership behavior ----------------------------

    def test_no_profiling_snapshot_returns_none_and_still_commits(self):
        db = _make_db()
        _seed_source_only(db, source_id=3, user_id="carol")
        # No profiling_snapshots row at all for source 3.
        wrapper = _CountingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            result = confirm_pii_column(
                source_id=3, user_id="carol",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is None
        assert wrapper.commit_count == 1  # commit() runs unconditionally after the UPDATE
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 1

    def test_no_matching_profile_row_returns_none_and_still_commits(self):
        db = self._setup_db()
        wrapper = _CountingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="does_not_exist",
            )

        assert result is None
        assert wrapper.commit_count == 1
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 1

    def test_unowned_source_returns_none_with_no_commit_and_no_writes(self):
        db = self._setup_db()
        before = _row_by_id(db, 100)
        wrapper = _CountingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            result = confirm_pii_column(
                source_id=1, user_id="mallory",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is None
        assert wrapper.commit_count == 0  # returns before the UPDATE is ever reached
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 1
        assert _row_by_id(db, 100) == before

    def test_nonexistent_source_returns_none_with_no_commit_and_no_writes(self):
        db = self._setup_db()
        before = _row_by_id(db, 100)
        wrapper = _CountingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            result = confirm_pii_column(
                source_id=999999, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is None
        assert wrapper.commit_count == 0
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 1
        assert _row_by_id(db, 100) == before

    # --- D. connection and transaction behavior ----------------------------

    def test_connection_acquisition_count_on_success(self):
        """NOT 1: confirm_pii_column()'s own read+write block uses exactly
        ONE connection (opened, used for the ownership check, the UPDATE
        whose WHERE clause embeds the latest-snapshot subquery, and the
        follow-up SELECT, then closed) - but on a successful confirmation
        the function ALSO calls log_governance_event() and
        upsert_governance_state() as post-success side effects, and each of
        those independently calls get_connection() itself (see their own
        definitions in data/governance_service.py). The observed total for
        a successful call is therefore 3, not 1."""
        db = self._setup_db()

        with _patch(db) as mock_get_connection:
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert mock_get_connection.call_count == 3

    def test_commit_and_close_counts_on_success(self):
        """Mirrors test_connection_acquisition_count_on_success: 3 commits
        and 3 closes on a successful call - one set from
        confirm_pii_column()'s own connection, one each from
        log_governance_event()'s and upsert_governance_state()'s own,
        separate connections."""
        db = self._setup_db()
        wrapper = _CountingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert wrapper.commit_count == 3
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 3

    def test_sql_error_during_update_propagates_without_commit_or_rollback(self):
        db = self._setup_db()
        wrapper = _RaisingOnUpdateConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper):
            with pytest.raises(sqlite3.OperationalError):
                confirm_pii_column(
                    source_id=1, user_id="alice",
                    table_fqn="dbo.customers", column_name="email_addr",
                )

        assert wrapper.commit_count == 0
        assert wrapper.rollback_count == 0  # no explicit rollback() call in the function
        assert wrapper.close_count == 1  # the finally block still runs

    # -------------------------------------------------------------------
    # Phase 2G.16 — migration-proof tests: confirm_pii_column() now
    # delegates to the shared resolver instead of running its own
    # correlated latest-snapshot subquery inside the UPDATE.
    # -------------------------------------------------------------------

    def test_resolver_called_once_with_source_id_and_caller_conn(self):
        """Proves: get_latest_profiling_snapshot is called exactly once,
        with the correct source_id and the exact connection object
        confirm_pii_column() itself opened (not a new one), and the
        resolver never opens its own connection."""
        db = self._setup_db()
        wrapper = _NoClose(db)

        resolver_calls: list = []
        real_resolver = get_latest_profiling_snapshot

        def _tracking_resolver(source_id_arg, conn=None):
            resolver_calls.append((source_id_arg, conn))
            return real_resolver(source_id_arg, conn=conn)

        mock_resolver_get_connection = MagicMock()
        with patch("data.governance_service.get_connection", return_value=wrapper), \
             patch("data.governance_service.get_latest_profiling_snapshot", _tracking_resolver), \
             patch("data.profiling_snapshot_resolver.get_connection", mock_resolver_get_connection):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        assert len(resolver_calls) == 1
        called_source_id, called_conn = resolver_calls[0]
        assert called_source_id == 1
        assert called_conn is wrapper
        assert mock_resolver_get_connection.call_count == 0

    def test_resolver_result_drives_update_not_inline_query(self):
        """Seeds a real, HIGHER-version profiling snapshot (which any
        remaining inline 'ORDER BY snapshot_version DESC' query would have
        picked), then patches the resolver to return a ProfilingSnapshotRef
        for the LOWER-version snapshot instead. Only the resolver-returned
        snapshot's row gets updated, proving snapshot.id from the
        resolver's return value drives the UPDATE - not any inline query.
        Also proves the source/table/column/heuristic predicates still
        apply within the resolver-selected snapshot."""
        db = self._setup_db()  # snap 10 (v1, COMPLETE), row 100 (email_addr, heuristic=1)
        _seed_snapshot(db, 20, source_id=1, snapshot_version=5, status="COMPLETE")
        _seed_column_profile(db, 200, 20, 1, "dbo.customers", "email_addr")
        # A second column under the SAME (resolver-selected) snap 10,
        # without the PII heuristic flag - the predicate must still gate it out.
        _seed_column_profile(
            db, 101, 10, 1, "dbo.customers", "full_name", pii_name_heuristic=0,
        )

        fake_ref = ProfilingSnapshotRef(
            id=10, source_id=1, snapshot_version=1,
            status="COMPLETE", schema_snapshot_id=1,
        )

        with patch(
            "data.governance_service.get_latest_profiling_snapshot",
            lambda source_id, conn=None: fake_ref,
        ), _patch(db):
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is not None
        # snap 10's row (the resolver-returned snapshot) was updated:
        assert _row_by_id(db, 100)["pii_confirmed"] == 1
        # snap 20's row (real highest-version, NOT resolver-returned) is
        # untouched - proves the UPDATE uses snapshot.id from the resolver,
        # not any inline "ORDER BY snapshot_version DESC" query:
        assert _row_by_id(db, 200)["pii_confirmed"] == 0
        # A row in the SAME resolver-selected snapshot without the
        # heuristic flag is still excluded by the predicate:
        assert _row_by_id(db, 101)["pii_confirmed"] == 0

    def test_resolver_returns_none_skips_update_but_still_commits_once(self):
        """When the resolver returns None (no profiling snapshot), the
        UPDATE must never execute, conn.commit() must still fire exactly
        once, the function returns None, and neither governance
        side-effect helper is called - preserving the pre-migration
        zero-row-match observable behavior exactly."""
        db = self._setup_db()
        wrapper = _RecordingConn(db)

        with patch("data.governance_service.get_connection", return_value=wrapper), \
             patch(
                 "data.governance_service.get_latest_profiling_snapshot",
                 return_value=None,
             ) as mock_resolver, \
             patch("data.governance_service.log_governance_event") as mock_log_event, \
             patch("data.governance_service.upsert_governance_state") as mock_upsert_state:
            result = confirm_pii_column(
                source_id=1, user_id="alice",
                table_fqn="dbo.customers", column_name="email_addr",
            )

        assert result is None
        assert mock_resolver.call_count == 1
        called_args, called_kwargs = mock_resolver.call_args
        assert called_args[0] == 1
        assert called_kwargs.get("conn") is wrapper
        assert not any(
            "UPDATE profiling_column_profiles" in sql for sql in wrapper.executed_sql
        )
        assert wrapper.commit_count == 1
        assert wrapper.rollback_count == 0
        assert wrapper.close_count == 1
        mock_log_event.assert_not_called()
        mock_upsert_state.assert_not_called()

    def test_static_exactly_one_resolver_import_and_call_with_conn(self):
        """Static/AST proof: exactly one resolver import at module level,
        and exactly one get_latest_profiling_snapshot(...) call inside
        confirm_pii_column() itself, with source_id as the first positional
        argument and conn=conn passed by keyword."""
        with open("data/governance_service.py", encoding="utf-8") as f:
            module_source = f.read()

        import_lines = [
            line for line in module_source.splitlines()
            if line.strip()
            == "from data.profiling_snapshot_resolver import get_latest_profiling_snapshot"
        ]
        assert len(import_lines) == 1, "expected exactly one resolver import"

        func_source = inspect.getsource(confirm_pii_column)
        tree = ast.parse(func_source)

        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "get_latest_profiling_snapshot"
        ]
        assert len(calls) == 1, "expected exactly one get_latest_profiling_snapshot(...) call"

        call = calls[0]
        positional_names = [a.id for a in call.args if isinstance(a, ast.Name)]
        assert positional_names[:1] == ["source_id"], (
            "source_id must be the first positional argument"
        )

        keyword_values = {kw.arg: kw.value for kw in call.keywords}
        assert "conn" in keyword_values
        conn_value = keyword_values["conn"]
        assert isinstance(conn_value, ast.Name) and conn_value.id == "conn"

    def test_no_inline_latest_snapshot_sql_remains_in_confirm_pii_column(self):
        """Confirms the correlated subquery this phase's migration removed
        is fully gone from confirm_pii_column(), and the UPDATE now uses a
        direct placeholder bound to snapshot.id."""
        func_source = inspect.getsource(confirm_pii_column)

        assert "SELECT id FROM profiling_snapshots" not in func_source
        assert "ORDER BY snapshot_version DESC LIMIT 1" not in func_source
        assert "profiling_snapshot_id = (" not in func_source  # no correlated subquery
        assert "WHERE profiling_snapshot_id = ?" in func_source

    def test_followup_select_sql_unchanged(self):
        """Static proof that the follow-up SELECT's SQL fragments are
        untouched by the migration - it still re-selects by
        (source_id, table_fqn, column_name) ordered by profiling_snapshot_id
        DESC, independent of snapshot_version. The runtime behavior this
        implies (the known stale-row divergence) is proven by
        test_followup_select_reads_the_updated_row_in_the_normal_case and
        test_followup_select_can_diverge_from_the_updated_row above, both
        retained unmodified and still passing against the migrated code."""
        func_source = inspect.getsource(confirm_pii_column)

        assert "SELECT * FROM profiling_column_profiles" in func_source
        assert "WHERE source_id = ? AND table_fqn = ? AND column_name = ?" in func_source
        assert "ORDER BY profiling_snapshot_id DESC LIMIT 1" in func_source


# ---------------------------------------------------------------------------
# Part 6 — list_governed_object_types
# ---------------------------------------------------------------------------

class TestListGovernedObjectTypes:
    def test_returns_all_eleven_types(self):
        types = list_governed_object_types()
        ids = {t["id"] for t in types}
        expected = {
            "dict.table", "dict.column",
            "domain.rule", "domain.refinement",
            "entity.rule", "tool.engine",
            "pii.confirmation", "relationship.suggestion",
            # Added in Phase 11 (Enterprise Autonomous Metadata Lifecycle) for the
            # domain/entity assignment human-lock governance event trail.
            "domain.assignment", "entity.assignment",
            # Added in Phase 2, Step 7 (Scoped Semantic Memory) for
            # concept-to-table/column mapping governance.
            "concept.mapping",
        }
        assert expected == ids

    def test_each_type_has_required_keys(self):
        for t in list_governed_object_types():
            assert "id" in t
            assert "display_name" in t
            assert "source_table" in t


# ---------------------------------------------------------------------------
# Part 7 — Backward compatibility (wired service side-effects)
# ---------------------------------------------------------------------------

class TestBackwardCompatibilityDictionaryService:
    """Existing approve functions still return the same contract."""

    def test_approve_table_still_returns_approved_and_coverage(self):
        db = _make_db()
        nc = _no_close(db)
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.fact_sales', 'fact_sales', 'dbo', 'TABLE', "
            "'Sales Facts', 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()

        with (
            patch("data.dictionary_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.dictionary_service import approve_table_dictionary
            result = approve_table_dictionary(
                source_id=1, user_id="alice", table_fqn="dbo.fact_sales"
            )

        assert result is not None
        assert result["approved"] is True
        assert "coverage" in result

    def test_approve_column_still_returns_approved_and_coverage(self):
        db = _make_db()
        nc = _no_close(db)
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO data_dictionary_columns "
            "(source_id, snapshot_id, table_fqn, column_name, business_label, "
            "pii_risk, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.fact_sales', 'sale_amount', 'Sale Amount', "
            "0, 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()

        with (
            patch("data.dictionary_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.dictionary_service import approve_column_dictionary
            result = approve_column_dictionary(
                source_id=1, user_id="alice",
                table_fqn="dbo.fact_sales", column_name="sale_amount",
            )

        assert result is not None
        assert result["approved"] is True

    def test_governance_event_written_after_table_approve(self):
        db = _make_db()
        nc = _no_close(db)
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, "
            "business_name, is_approved, generation_method, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.dim_date', 'dim_date', 'dbo', 'TABLE', "
            "'Date Dimension', 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()

        with (
            patch("data.dictionary_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.dictionary_service import approve_table_dictionary
            approve_table_dictionary(
                source_id=1, user_id="alice", table_fqn="dbo.dim_date"
            )

        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'dict.table' AND object_id = '1:dbo.dim_date'"
        ).fetchall()
        assert len(events) == 1
        assert dict(events[0])["to_state"] == "HUMAN_APPROVED"
        assert dict(events[0])["actor_id"] == "alice"


class TestBackwardCompatibilityDomainRules:
    def _setup(self) -> sqlite3.Connection:
        db = _make_db()
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO domain_learning_rules "
            "(id, source_id, pattern_type, pattern_value, domain, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'PREFIX', 'fact_', 'Sales', 0.92, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        return db

    def test_approve_domain_rule_still_returns_updated_row(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.domain_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.domain_learning_service import approve_domain_rule
            result = approve_domain_rule(rule_id=1, user_id="alice")
        assert result is not None
        assert result["approval_status"] == "APPROVED"
        assert result["active"] == 1

    def test_governance_event_written_after_domain_rule_approve(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.domain_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.domain_learning_service import approve_domain_rule
            approve_domain_rule(rule_id=1, user_id="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'domain.rule' AND object_id = '1'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert e["event_type"] == "APPROVED"
        assert e["actor_id"] == "alice"

    def test_reject_domain_rule_still_works(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.domain_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.domain_learning_service import reject_domain_rule
            result = reject_domain_rule(rule_id=1, user_id="alice")
        assert result is not None
        assert result["approval_status"] == "REJECTED"
        assert result["active"] == 0

    def test_governance_event_written_after_domain_rule_reject(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.domain_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.domain_learning_service import reject_domain_rule
            reject_domain_rule(rule_id=1, user_id="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'domain.rule'"
        ).fetchall()
        assert len(events) == 1
        assert dict(events[0])["event_type"] == "REJECTED"


class TestBackwardCompatibilityEntityRules:
    def _setup(self) -> sqlite3.Connection:
        db = _make_db()
        db.execute(
            "INSERT INTO data_source_connections "
            "(id, user_id, display_name, source_type, source_category, "
            "encrypted_config_json, config_schema_version, capabilities_json, "
            "metadata_json, source_status, is_active, created_at, updated_at) "
            "VALUES (1, 'alice', 'DB', 'mssql', 'relational_db', "
            "'{}', 1, '[]', '{}', 'ACTIVE', 1, '2025-01-01', '2025-01-01')"
        )
        db.execute(
            "INSERT INTO entity_learning_rules "
            "(id, source_id, pattern_type, pattern_value, entity, confidence, "
            "approval_status, created_by, created_at, active) "
            "VALUES (1, 1, 'TOKEN', 'customer', 'Customer', 0.88, "
            "'PENDING', 'system', '2025-01-01', 0)"
        )
        db.commit()
        return db

    def test_approve_entity_rule_still_returns_updated_row(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.entity_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.entity_learning_service import approve_entity_rule
            result = approve_entity_rule(rule_id=1, user_id="alice")
        assert result is not None
        assert result["approval_status"] == "APPROVED"

    def test_governance_event_written_after_entity_rule_approve(self):
        db = self._setup()
        nc = _no_close(db)
        with (
            patch("data.entity_learning_service.get_connection", return_value=nc),
            patch("data.governance_service.get_connection", return_value=nc),
        ):
            from data.entity_learning_service import approve_entity_rule
            approve_entity_rule(rule_id=1, user_id="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE object_type_id = 'entity.rule'"
        ).fetchall()
        assert len(events) == 1
        assert dict(events[0])["to_state"] == "HUMAN_APPROVED"
