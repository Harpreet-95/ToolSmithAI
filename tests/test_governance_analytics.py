"""
Tests for Phase 6 — Governance Analytics & Executive Dashboard Backend.

Covers:
  - governance_kpis() — state-distribution percentages, assignment backlog
  - governance_trends() — placeholder trend shape + today's velocity
  - governance_bottlenecks() — pending queues, low confidence, overdue stewards
  - governance_recommendations() — rule-based recommendation generation
  - get_governance_dashboard() — full aggregation
  - Empty-DB / unknown-source safety
  - Backward compatibility (no existing function signatures changed)

Run from project root:
    venv/Scripts/pytest tests/test_governance_analytics.py -v
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-analytics-long-enough-32chars")
os.environ.setdefault("USER_ID_SALT", "test-salt-analytics-phase6")

from data.governance_service import (
    governance_kpis,
    governance_trends,
    governance_bottlenecks,
    governance_recommendations,
    get_governance_dashboard,
    governance_readiness_summary,
    calculate_risk_score,
    recommend_next_action,
)


# ---------------------------------------------------------------------------
# Schema (same shape as Phase 3-5 governance test suites)
# ---------------------------------------------------------------------------

_SCHEMA = """
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_from_now(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).date().isoformat()


def _insert_state(db, object_type_id, object_id, approval_state,
                   confidence_score=0.8, confidence_tier="HIGH"):
    db.execute(
        """INSERT INTO governance_state_map
               (object_type_id, object_id, approval_state, confidence_score,
                confidence_tier, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (object_type_id, object_id, approval_state, confidence_score,
         confidence_tier, _now_iso(), _now_iso()),
    )
    db.commit()


def _insert_assignment(db, object_type="domain.rule", object_id="1", source_id=1,
                        assigned_to="alice", priority="MEDIUM", status="OPEN",
                        due_date=None, completed_at=None, created_at=None):
    db.execute(
        """INSERT INTO governance_assignments
               (object_type, object_id, source_id, assigned_to, assigned_by,
                priority, status, due_date, created_at, updated_at, completed_at)
           VALUES (?, ?, ?, ?, 'system', ?, ?, ?, ?, ?, ?)""",
        (object_type, object_id, source_id, assigned_to, priority, status,
         due_date, created_at or _now_iso(), _now_iso(), completed_at),
    )
    db.commit()


def _insert_event(db, object_type_id="domain.rule", object_id="1",
                   event_type="APPROVED", to_state="HUMAN_APPROVED", created_at=None):
    db.execute(
        """INSERT INTO governance_approval_events
               (object_type_id, object_id, event_type, to_state, actor_id, created_at)
           VALUES (?, ?, ?, ?, 'tester', ?)""",
        (object_type_id, object_id, event_type, to_state, created_at or _now_iso()),
    )
    db.commit()


# ---------------------------------------------------------------------------
# governance_kpis
# ---------------------------------------------------------------------------

class TestGovernanceKPIs:
    def test_empty_db_returns_zeros(self):
        db = _make_db()
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["total_governed"] == 0
        assert kpis["human_approved_pct"] == 0.0
        assert kpis["avg_confidence"] is None
        assert kpis["avg_risk_score"] is None
        assert kpis["open_assignments"] == 0

    def test_state_percentages_sum_to_roughly_100(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "HUMAN_APPROVED")
        _insert_state(db, "domain.rule", "2", "AUTO_APPROVED")
        _insert_state(db, "domain.rule", "3", "SUGGESTED")
        _insert_state(db, "domain.rule", "4", "NEEDS_REVIEW")
        with _patch(db):
            kpis = governance_kpis()
        total_pct = (kpis["human_approved_pct"] + kpis["auto_approved_pct"]
                     + kpis["pending_pct"] + kpis["escalated_pct"])
        assert 99.0 <= total_pct <= 100.1
        assert kpis["total_governed"] == 4

    def test_pending_includes_suggested_generated_validated(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        _insert_state(db, "domain.rule", "2", "GENERATED")
        _insert_state(db, "domain.rule", "3", "VALIDATED")
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["pending_pct"] == 100.0

    def test_blocked_includes_rejected_deprecated_archived(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "REJECTED")
        _insert_state(db, "domain.rule", "2", "DEPRECATED")
        _insert_state(db, "domain.rule", "3", "ARCHIVED")
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["blocked_pct"] == 100.0

    def test_avg_confidence_weighted(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED", confidence_score=0.5)
        _insert_state(db, "domain.rule", "2", "SUGGESTED", confidence_score=1.0)
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["avg_confidence"] == pytest.approx(0.75, abs=0.01)

    def test_open_assignments_counted(self):
        db = _make_db()
        _insert_assignment(db, status="OPEN")
        _insert_assignment(db, status="OPEN")
        _insert_assignment(db, status="COMPLETED")
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["open_assignments"] == 2

    def test_overdue_assignments_counted(self):
        db = _make_db()
        _insert_assignment(db, status="OPEN", due_date=_days_from_now(-2))
        _insert_assignment(db, status="OPEN", due_date=_days_from_now(5))
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["overdue_assignments"] == 1

    def test_critical_backlog_counted(self):
        db = _make_db()
        _insert_assignment(db, status="OPEN", priority="CRITICAL")
        _insert_assignment(db, status="OPEN", priority="LOW")
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["critical_backlog"] == 1

    def test_avg_resolution_days_computed(self):
        db = _make_db()
        created = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        _insert_assignment(db, status="COMPLETED", created_at=created,
                            completed_at=_now_iso())
        with _patch(db):
            kpis = governance_kpis()
        assert kpis["avg_resolution_days"] is not None
        assert kpis["avg_resolution_days"] >= 2.9

    def test_source_id_scopes_assignments_not_state_distribution(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        _insert_assignment(db, source_id=1, status="OPEN")
        _insert_assignment(db, source_id=2, status="OPEN")
        with _patch(db):
            kpis_src1 = governance_kpis(source_id=1)
        assert kpis_src1["open_assignments"] == 1
        assert kpis_src1["total_governed"] == 1  # state map is global


# ---------------------------------------------------------------------------
# governance_trends
# ---------------------------------------------------------------------------

class TestGovernanceTrends:
    def test_trend_available_is_false(self):
        db = _make_db()
        with _patch(db):
            trends = governance_trends()
        assert trends["trend_available"] is False

    def test_current_snapshot_has_required_keys(self):
        db = _make_db()
        with _patch(db):
            trends = governance_trends()
        snap = trends["current_snapshot"]
        for key in ("timestamp", "governance_score", "objects_ready",
                    "objects_pending", "objects_blocked", "objects_escalated",
                    "avg_confidence", "open_assignments"):
            assert key in snap

    def test_trend_7d_and_30d_are_empty_lists(self):
        db = _make_db()
        with _patch(db):
            trends = governance_trends()
        assert trends["trend_7d"] == []
        assert trends["trend_30d"] == []

    def test_velocity_has_required_keys(self):
        db = _make_db()
        with _patch(db):
            trends = governance_trends()
        v = trends["velocity"]
        assert set(v.keys()) == {
            "approvals_today", "rejections_today", "assignments_completed_today"
        }

    def test_approvals_today_counted_from_events(self):
        db = _make_db()
        _insert_event(db, to_state="HUMAN_APPROVED")
        _insert_event(db, to_state="AUTO_APPROVED")
        _insert_event(db, to_state="REJECTED")
        with _patch(db):
            trends = governance_trends()
        assert trends["velocity"]["approvals_today"] == 2
        assert trends["velocity"]["rejections_today"] == 1


# ---------------------------------------------------------------------------
# governance_bottlenecks
# ---------------------------------------------------------------------------

class TestGovernanceBottlenecks:
    def test_empty_db_returns_empty_lists(self):
        db = _make_db()
        with _patch(db):
            b = governance_bottlenecks()
        assert b["pending_by_type"] == []
        assert b["overdue_stewards"] == []

    def test_pending_by_type_excludes_terminal_states(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        _insert_state(db, "domain.rule", "2", "HUMAN_APPROVED")
        with _patch(db):
            b = governance_bottlenecks()
        types = {row["object_type"]: row["pending_count"] for row in b["pending_by_type"]}
        assert types.get("domain.rule") == 1

    def test_low_confidence_areas_detected(self):
        db = _make_db()
        _insert_state(db, "dict.column", "1", "SUGGESTED",
                       confidence_score=0.3, confidence_tier="LOW")
        with _patch(db):
            b = governance_bottlenecks()
        assert any(r["object_type"] == "dict.column" for r in b["low_confidence_areas"])

    def test_needs_review_by_type(self):
        db = _make_db()
        _insert_state(db, "dict.table", "1", "NEEDS_REVIEW")
        with _patch(db):
            b = governance_bottlenecks()
        assert any(r["object_type"] == "dict.table" for r in b["needs_review_by_type"])

    def test_overdue_stewards_detected(self):
        db = _make_db()
        _insert_assignment(db, assigned_to="bob", status="OPEN",
                            due_date=_days_from_now(-5))
        with _patch(db):
            b = governance_bottlenecks()
        assert len(b["overdue_stewards"]) == 1
        assert b["overdue_stewards"][0]["assigned_to"] == "bob"
        assert b["overdue_stewards"][0]["oldest_days_overdue"] >= 4

    def test_active_blocking_policies_listed(self):
        db = _make_db()
        db.execute(
            """INSERT INTO governance_policies
                   (policy_name, enabled, priority, object_types_json,
                    condition_json, action, created_at, updated_at)
               VALUES ('TEST_REQUIRE_HUMAN', 1, 10, '[]', '{}', 'REQUIRE_HUMAN', ?, ?)""",
            (_now_iso(), _now_iso()),
        )
        db.commit()
        with _patch(db):
            b = governance_bottlenecks()
        names = {p["policy_name"] for p in b["active_blocking_policies"]}
        assert "TEST_REQUIRE_HUMAN" in names

    def test_pending_domains_listed(self):
        db = _make_db()
        db.execute(
            """INSERT INTO domain_learning_rules
                   (source_id, pattern_type, pattern_value, domain, confidence,
                    approval_status, created_at)
               VALUES (1, 'TOKEN', 'rev', 'Revenue', 0.8, 'PENDING', ?)""",
            (_now_iso(),),
        )
        db.commit()
        with _patch(db):
            b = governance_bottlenecks()
        domains = {d["domain"] for d in b["pending_domains"]}
        assert "Revenue" in domains

    def test_pending_entities_listed(self):
        db = _make_db()
        db.execute(
            """INSERT INTO entity_learning_rules
                   (source_id, pattern_type, pattern_value, entity, confidence,
                    approval_status, created_at)
               VALUES (1, 'TOKEN', 'cust', 'Customer', 0.8, 'PENDING', ?)""",
            (_now_iso(),),
        )
        db.commit()
        with _patch(db):
            b = governance_bottlenecks()
        entities = {e["entity"] for e in b["pending_entities"]}
        assert "Customer" in entities


# ---------------------------------------------------------------------------
# governance_recommendations
# ---------------------------------------------------------------------------

class TestGovernanceRecommendations:
    def test_no_data_returns_empty_or_minimal(self):
        db = _make_db()
        with _patch(db):
            recs = governance_recommendations()
        assert isinstance(recs, list)

    def test_pii_backlog_triggers_critical_recommendation(self):
        db = _make_db()
        db.execute(
            """INSERT INTO data_dictionary_columns
                   (source_id, table_fqn, column_name, pii_risk, is_approved)
               VALUES (1, 'dbo.Customers', 'ssn', 1, 0)"""
        )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        pii_recs = [r for r in recs if r["id"] == "REVIEW_PII_BACKLOG"]
        assert len(pii_recs) == 1
        assert pii_recs[0]["priority"] == "CRITICAL"
        assert pii_recs[0]["affected_count"] == 1

    def test_critical_assignments_trigger_recommendation(self):
        db = _make_db()
        _insert_assignment(db, status="OPEN", priority="CRITICAL")
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "CLEAR_CRITICAL_BACKLOG" for r in recs)

    def test_overdue_triggers_recommendation(self):
        db = _make_db()
        _insert_assignment(db, status="OPEN", due_date=_days_from_now(-3))
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "ADDRESS_OVERDUE_ASSIGNMENTS" for r in recs)

    def test_finance_domain_triggers_escalation(self):
        db = _make_db()
        db.execute(
            """INSERT INTO domain_learning_rules
                   (source_id, pattern_type, pattern_value, domain, confidence,
                    approval_status, created_at)
               VALUES (1, 'TOKEN', 'fin', 'Finance', 0.7, 'PENDING', ?)""",
            (_now_iso(),),
        )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "ESCALATE_FINANCE_APPROVALS" for r in recs)

    def test_high_confidence_rules_trigger_bulk_approve(self):
        db = _make_db()
        for i in range(6):
            db.execute(
                """INSERT INTO domain_learning_rules
                       (source_id, pattern_type, pattern_value, domain, confidence,
                        approval_status, created_at)
                   VALUES (1, 'TOKEN', ?, 'Sales', 0.97, 'PENDING', ?)""",
                (f"tok{i}", _now_iso()),
            )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "BULK_APPROVE_HIGH_CONFIDENCE" for r in recs)

    def test_few_high_confidence_rules_does_not_trigger(self):
        db = _make_db()
        db.execute(
            """INSERT INTO domain_learning_rules
                   (source_id, pattern_type, pattern_value, domain, confidence,
                    approval_status, created_at)
               VALUES (1, 'TOKEN', 'a', 'Sales', 0.97, 'PENDING', ?)""",
            (_now_iso(),),
        )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        assert not any(r["id"] == "BULK_APPROVE_HIGH_CONFIDENCE" for r in recs)

    def test_large_dict_backlog_triggers_review(self):
        db = _make_db()
        for i in range(11):
            db.execute(
                """INSERT INTO data_dictionary_tables
                       (source_id, table_fqn, table_name, business_name, is_approved)
                   VALUES (1, ?, ?, 'Some Table', 0)""",
                (f"dbo.T{i}", f"T{i}"),
            )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "REVIEW_DICTIONARY_ENTRIES" for r in recs)

    def test_metadata_gap_triggers_improvement(self):
        db = _make_db()
        db.execute(
            """INSERT INTO data_dictionary_tables
                   (source_id, table_fqn, table_name, business_name, is_approved)
               VALUES (1, 'dbo.T1', 'T1', NULL, 0)"""
        )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "IMPROVE_METADATA_COVERAGE" for r in recs)

    def test_unassigned_suggested_items_trigger_assignment(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "ASSIGN_STEWARDS" for r in recs)

    def test_assigned_suggested_item_does_not_trigger(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        _insert_assignment(db, object_type="domain.rule", object_id="1", status="OPEN")
        with _patch(db):
            recs = governance_recommendations()
        assert not any(r["id"] == "ASSIGN_STEWARDS" for r in recs)

    def test_object_id_collision_across_types_does_not_false_negative(self):
        # dict.table id "1" assigned should NOT suppress domain.rule id "1" unassigned
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        _insert_assignment(db, object_type="dict.table", object_id="1", status="OPEN")
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "ASSIGN_STEWARDS" for r in recs)

    def test_recommendations_sorted_by_priority(self):
        db = _make_db()
        db.execute(
            """INSERT INTO data_dictionary_columns
                   (source_id, table_fqn, column_name, pii_risk, is_approved)
               VALUES (1, 'dbo.Customers', 'ssn', 1, 0)"""
        )
        _insert_assignment(db, status="OPEN", due_date=_days_from_now(-3))
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        priorities = [r["priority"] for r in recs]
        order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        assert priorities == sorted(priorities, key=lambda p: order[p])

    def test_truly_empty_db_does_not_trigger_coverage_warning(self):
        # No governed objects at all is "nothing to govern yet", not "coverage
        # is critically low" — governance_score is 0/100 in both cases, so the
        # recommendation must additionally require total_governed > 0.
        db = _make_db()
        with _patch(db):
            recs = governance_recommendations()
        assert not any(r["id"] == "IMPROVE_GOVERNANCE_COVERAGE" for r in recs)

    def test_low_score_with_governed_objects_triggers_coverage_warning(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "NEEDS_REVIEW")
        _insert_state(db, "domain.rule", "2", "REJECTED")
        with _patch(db):
            recs = governance_recommendations()
        assert any(r["id"] == "IMPROVE_GOVERNANCE_COVERAGE" for r in recs)

    def test_every_recommendation_has_required_fields(self):
        db = _make_db()
        db.execute(
            """INSERT INTO data_dictionary_columns
                   (source_id, table_fqn, column_name, pii_risk, is_approved)
               VALUES (1, 'dbo.Customers', 'ssn', 1, 0)"""
        )
        db.commit()
        with _patch(db):
            recs = governance_recommendations()
        for r in recs:
            for key in ("id", "title", "description", "priority",
                        "affected_count", "action_endpoint", "action_params"):
                assert key in r


# ---------------------------------------------------------------------------
# get_governance_dashboard
# ---------------------------------------------------------------------------

class TestGovernanceDashboard:
    def test_returns_all_sections(self):
        db = _make_db()
        with _patch(db):
            dash = get_governance_dashboard()
        for key in ("generated_at", "source_id", "executive_summary",
                    "kpis", "trends", "bottlenecks", "recommendations"):
            assert key in dash

    def test_empty_db_no_crash(self):
        db = _make_db()
        with _patch(db):
            dash = get_governance_dashboard()
        assert dash["kpis"]["total_governed"] == 0
        assert dash["recommendations"] == []

    def test_source_id_propagated(self):
        db = _make_db()
        with _patch(db):
            dash = get_governance_dashboard(source_id=7)
        assert dash["source_id"] == 7

    def test_dashboard_integrates_real_data(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "HUMAN_APPROVED")
        _insert_state(db, "domain.rule", "2", "NEEDS_REVIEW")
        _insert_assignment(db, status="OPEN", priority="CRITICAL")
        with _patch(db):
            dash = get_governance_dashboard()
        assert dash["kpis"]["total_governed"] == 2
        assert dash["kpis"]["critical_backlog"] == 1
        assert any(r["id"] == "CLEAR_CRITICAL_BACKLOG" for r in dash["recommendations"])


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    def test_readiness_summary_unaffected(self):
        db = _make_db()
        with _patch(db):
            summary = governance_readiness_summary()
        assert "governance_score" in summary

    def test_risk_score_and_next_action_still_importable(self):
        assert callable(calculate_risk_score)
        assert callable(recommend_next_action)

    def test_analytics_functions_are_read_only_no_writes(self):
        db = _make_db()
        _insert_state(db, "domain.rule", "1", "SUGGESTED")
        before = db.execute("SELECT COUNT(*) FROM governance_state_map").fetchone()[0]
        with _patch(db):
            governance_kpis()
            governance_trends()
            governance_bottlenecks()
            governance_recommendations()
            get_governance_dashboard()
        after_state = db.execute("SELECT COUNT(*) FROM governance_state_map").fetchone()[0]
        after_assign = db.execute("SELECT COUNT(*) FROM governance_assignments").fetchone()[0]
        after_events = db.execute("SELECT COUNT(*) FROM governance_approval_events").fetchone()[0]
        assert after_state == before
        assert after_assign == 0
        assert after_events == 0
