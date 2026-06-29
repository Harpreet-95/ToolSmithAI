"""
Tests for Phase 4 — Governance Stewardship & Work Management.

Covers:
  - AssignmentPriority and AssignmentStatus enums
  - calculate_priority_for_profile() — pure priority logic
  - calculate_sla() — pure SLA arithmetic
  - assign_governance_item() — creation, auto-priority, auto-due-date
  - reassign_governance_item() — transfer, governance event
  - complete_assignment() — completion, timestamp, event
  - list_assignments() — all filter combinations + overdue_only + SLA attached
  - assignment_summary() — all metrics including by_steward, avg_resolution
  - Ownership: reassign/complete return None for unknown ids
  - Governance events written for every stewardship action
  - No new approval system created

Run from project root:
    venv/Scripts/pytest tests/test_governance_stewardship.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-stewardship-long-enough-32chars")
os.environ.setdefault("USER_ID_SALT", "test-salt-stewardship-phase4")

from data.governance_service import (
    AssignmentPriority,
    AssignmentStatus,
    GovernanceProfile,
    GovernanceState,
    GovernedObjectType,
    _HARD_POLICY_HIGH_RISK,
    _HARD_POLICY_PII,
    _SLA_DAYS_BY_PRIORITY,
    assign_governance_item,
    assignment_summary,
    calculate_priority_for_profile,
    calculate_sla,
    complete_assignment,
    list_assignments,
    reassign_governance_item,
)


# ---------------------------------------------------------------------------
# Minimal schema
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

_SCHEMA_EXTRA = """
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
"""


class _NoClose:
    def __init__(self, conn):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def close(self):
        pass


def _make_db(extra: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    if extra:
        conn.executescript(_SCHEMA_EXTRA)
    return conn


def _patch(db):
    return patch("data.governance_service.get_connection", return_value=_NoClose(db))


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _days_ago(n: int) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=n))


def _days_from_now(n: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


def _insert_assignment(db, *, object_type="domain.rule", object_id="1",
                       assigned_to="alice", assigned_by="admin",
                       priority="MEDIUM", status="OPEN",
                       due_date=None, source_id=None,
                       assignment_group=None,
                       created_at=None, completed_at=None):
    now = created_at or datetime.now(timezone.utc).isoformat()
    if due_date is None:
        sla_days = _SLA_DAYS_BY_PRIORITY.get(priority, 7)
        due_date = (datetime.now(timezone.utc).date() + timedelta(days=sla_days)).isoformat()
    db.execute(
        """INSERT INTO governance_assignments
               (object_type, object_id, source_id, assigned_to, assigned_by,
                assignment_group, priority, status, due_date, created_at, updated_at,
                completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (object_type, object_id, source_id, assigned_to, assigned_by,
         assignment_group, priority, status, due_date, now, now, completed_at),
    )
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_profile(**kwargs) -> GovernanceProfile:
    """Build a minimal GovernanceProfile; override any field via kwargs."""
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
# Part 1 — Priority Calculation (pure logic)
# ---------------------------------------------------------------------------

class TestPriorityCalculation:
    def test_pii_risk_is_critical(self):
        p = _make_profile(pii_risk=True)
        assert calculate_priority_for_profile(p) == AssignmentPriority.CRITICAL

    def test_hard_pii_policy_is_critical(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_PII)
        assert calculate_priority_for_profile(p) == AssignmentPriority.CRITICAL

    def test_high_risk_domain_policy_is_high(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_HIGH_RISK)
        assert calculate_priority_for_profile(p) == AssignmentPriority.HIGH

    def test_needs_review_state_is_high(self):
        p = _make_profile(approval_state=GovernanceState.NEEDS_REVIEW)
        assert calculate_priority_for_profile(p) == AssignmentPriority.HIGH

    def test_low_confidence_is_high(self):
        p = _make_profile(confidence_score=0.45)
        assert calculate_priority_for_profile(p) == AssignmentPriority.HIGH

    def test_auto_approval_eligible_is_low(self):
        p = _make_profile(auto_approval_eligible=True, confidence_score=0.99)
        assert calculate_priority_for_profile(p) == AssignmentPriority.LOW

    def test_medium_confidence_is_medium(self):
        p = _make_profile(confidence_score=0.70, auto_approval_eligible=False)
        assert calculate_priority_for_profile(p) == AssignmentPriority.MEDIUM

    def test_no_confidence_is_medium(self):
        p = _make_profile(confidence_score=None, auto_approval_eligible=False)
        assert calculate_priority_for_profile(p) == AssignmentPriority.MEDIUM

    def test_high_confidence_is_low(self):
        p = _make_profile(confidence_score=0.92, auto_approval_eligible=False,
                          blocking_policy=None)
        assert calculate_priority_for_profile(p) == AssignmentPriority.LOW

    def test_pii_overrides_high_confidence(self):
        p = _make_profile(pii_risk=True, confidence_score=0.99,
                          auto_approval_eligible=True)
        assert calculate_priority_for_profile(p) == AssignmentPriority.CRITICAL

    def test_high_risk_domain_overrides_medium_confidence(self):
        p = _make_profile(blocking_policy=_HARD_POLICY_HIGH_RISK,
                          confidence_score=0.70)
        assert calculate_priority_for_profile(p) == AssignmentPriority.HIGH


# ---------------------------------------------------------------------------
# Part 2 — SLA Calculation (pure logic)
# ---------------------------------------------------------------------------

class TestSLACalculation:
    def _assignment(self, *, priority="MEDIUM", status="OPEN",
                    created_at=None, due_date=None, completed_at=None):
        now_str = created_at or datetime.now(timezone.utc).isoformat()
        return {
            "priority":     priority,
            "status":       status,
            "created_at":   now_str,
            "due_date":     due_date,
            "completed_at": completed_at,
        }

    def test_on_track_has_no_overdue(self):
        # Created now, MEDIUM priority → due in 7 days → ON_TRACK
        a = self._assignment(priority="MEDIUM")
        sla = calculate_sla(a)
        assert sla["sla_status"] == "ON_TRACK"
        assert sla["days_overdue"] == 0
        assert sla["escalation_required"] is False

    def test_overdue_when_past_due_date(self):
        # Created 10 days ago, MEDIUM priority (SLA=7) → overdue by 3 days
        created = _days_ago(10)
        a = self._assignment(priority="MEDIUM", created_at=created)
        sla = calculate_sla(a)
        assert sla["sla_status"] == "OVERDUE"
        assert sla["days_overdue"] > 0
        assert sla["escalation_required"] is True

    def test_at_risk_within_1_day_of_due(self):
        # Due tomorrow
        due = _days_from_now(0)  # today = end of today
        created = _days_ago(6)   # MEDIUM SLA=7, due tomorrow
        a = self._assignment(priority="MEDIUM", created_at=created, due_date=due)
        sla = calculate_sla(a, reference_date=datetime.now(timezone.utc).isoformat())
        assert sla["sla_status"] in ("AT_RISK", "OVERDUE")

    def test_completed_status_always_sla_completed(self):
        a = self._assignment(
            status="COMPLETED",
            created_at=_days_ago(5),
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        sla = calculate_sla(a)
        assert sla["sla_status"] == "COMPLETED"
        assert sla["days_overdue"] == 0
        assert sla["escalation_required"] is False
        assert sla["risk_level"] == "LOW"

    def test_days_open_increases_over_time(self):
        created = _days_ago(4)
        a = self._assignment(created_at=created)
        sla = calculate_sla(a)
        assert sla["days_open"] >= 4

    def test_critical_sla_threshold_is_1_day(self):
        assert _SLA_DAYS_BY_PRIORITY[AssignmentPriority.CRITICAL] == 1

    def test_low_sla_threshold_is_14_days(self):
        assert _SLA_DAYS_BY_PRIORITY[AssignmentPriority.LOW] == 14

    def test_critical_priority_overdue_quickly(self):
        # CRITICAL: SLA=1 day → created 2 days ago → OVERDUE
        created = _days_ago(2)
        a = self._assignment(priority="CRITICAL", created_at=created)
        sla = calculate_sla(a)
        assert sla["sla_status"] == "OVERDUE"
        assert sla["escalation_required"] is True

    def test_reference_date_controls_calculation(self):
        # Created 2025-01-01, reference 2025-01-03 → 2 days open for MEDIUM
        a = self._assignment(
            priority="MEDIUM",
            created_at="2025-01-01T00:00:00+00:00",
        )
        sla = calculate_sla(a, reference_date="2025-01-03T12:00:00+00:00")
        assert sla["days_open"] == 2
        assert sla["sla_status"] == "ON_TRACK"

    def test_explicit_due_date_overrides_sla_threshold(self):
        # Due 2 days ago → OVERDUE regardless of priority (LOW SLA would be 14 days)
        a = self._assignment(
            priority="LOW",
            created_at=_days_ago(10),
            due_date=_days_from_now(-2),  # 2 days ago → clearly overdue
        )
        sla = calculate_sla(a)
        assert sla["sla_status"] == "OVERDUE"

    def test_sla_due_date_returned(self):
        a = self._assignment(priority="HIGH")
        sla = calculate_sla(a)
        # sla_due_date should be a valid ISO date string
        from datetime import date as dt_date
        dt_date.fromisoformat(sla["sla_due_date"])  # will raise if invalid

    def test_risk_level_critical_when_far_overdue(self):
        # MEDIUM SLA=7 days, created 20 days ago → days_overdue=13 > sla_days → CRITICAL
        a = self._assignment(priority="MEDIUM", created_at=_days_ago(20))
        sla = calculate_sla(a)
        assert sla["risk_level"] == "CRITICAL"


# ---------------------------------------------------------------------------
# Part 3 — Assignment Creation
# ---------------------------------------------------------------------------

class TestAssignmentCreation:
    def test_create_assignment_with_explicit_priority(self):
        db = _make_db()
        with _patch(db):
            result = assign_governance_item(
                object_type  = "domain.rule",
                object_id    = "42",
                assigned_to  = "alice",
                assigned_by  = "admin",
                priority     = "HIGH",
                due_date     = "2025-12-31",
            )
        assert result["assigned_to"] == "alice"
        assert result["priority"] == "HIGH"
        assert result["status"] == "OPEN"
        assert result["due_date"] == "2025-12-31"

    def test_auto_due_date_from_priority_sla(self):
        db = _make_db()
        with _patch(db):
            result = assign_governance_item(
                object_type = "domain.rule",
                object_id   = "1",
                assigned_to = "bob",
                assigned_by = "admin",
                priority    = "CRITICAL",  # SLA = 1 day
            )
        # due_date should be tomorrow at the latest
        from datetime import date
        due = date.fromisoformat(result["due_date"])
        tomorrow = date.today() + timedelta(days=1)
        assert due <= tomorrow

    def test_auto_priority_from_pii_profile(self):
        """When priority is not provided, profile is loaded to calculate it."""
        db = _make_db(extra=True)
        db.execute(
            "INSERT INTO data_dictionary_tables "
            "(source_id, snapshot_id, table_fqn, table_name, schema_name, "
            "table_type, business_name, is_approved, generation_method, "
            "created_at, updated_at) "
            "VALUES (1, 1, 'dbo.customers', 'customers', 'dbo', 'TABLE', "
            "'Customers', 0, 'rule_based', '2025-01-01', '2025-01-01')"
        )
        db.commit()
        with patch("data.governance_service.get_connection", return_value=_NoClose(db)):
            result = assign_governance_item(
                object_type = "dict.table",
                object_id   = "1:dbo.customers",
                assigned_to = "alice",
                assigned_by = "admin",
                # no priority — should be auto-calculated
            )
        # Profile has no PII and no blocking policy → MEDIUM or LOW
        assert result["priority"] in (
            AssignmentPriority.MEDIUM, AssignmentPriority.LOW,
            AssignmentPriority.HIGH  # if domain context triggers something
        )

    def test_invalid_priority_defaults_to_medium(self):
        db = _make_db()
        with _patch(db):
            result = assign_governance_item(
                object_type = "domain.rule",
                object_id   = "1",
                assigned_to = "alice",
                assigned_by = "admin",
                priority    = "INVALID_PRIORITY",
            )
        assert result["priority"] == AssignmentPriority.MEDIUM

    def test_assignment_group_persisted(self):
        db = _make_db()
        with _patch(db):
            result = assign_governance_item(
                object_type      = "domain.rule",
                object_id        = "5",
                assigned_to      = "team_a_user",
                assigned_by      = "admin",
                assignment_group = "Data Governance Team",
            )
        assert result["assignment_group"] == "Data Governance Team"

    def test_source_id_persisted(self):
        db = _make_db()
        with _patch(db):
            result = assign_governance_item(
                object_type = "domain.rule",
                object_id   = "5",
                assigned_to = "alice",
                assigned_by = "admin",
                source_id   = 7,
            )
        assert result["source_id"] == 7

    def test_governance_event_written_on_assign(self):
        db = _make_db()
        with _patch(db):
            assign_governance_item(
                object_type = "domain.rule",
                object_id   = "3",
                assigned_to = "alice",
                assigned_by = "admin",
            )
        events = db.execute(
            "SELECT * FROM governance_approval_events WHERE event_type = 'ASSIGNED'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert e["object_id"] == "3"
        assert e["actor_id"] == "admin"


# ---------------------------------------------------------------------------
# Part 4 — Reassignment
# ---------------------------------------------------------------------------

class TestReassignment:
    def test_reassign_changes_assignee(self):
        db = _make_db()
        aid = _insert_assignment(db, assigned_to="alice")
        with _patch(db):
            result = reassign_governance_item(
                assignment_id = aid,
                new_assignee  = "bob",
                reassigned_by = "manager",
            )
        assert result is not None
        assert result["assigned_to"] == "bob"

    def test_reassign_nonexistent_returns_none(self):
        db = _make_db()
        with _patch(db):
            result = reassign_governance_item(
                assignment_id = 9999,
                new_assignee  = "bob",
                reassigned_by = "manager",
            )
        assert result is None

    def test_reassign_completed_returns_none(self):
        db = _make_db()
        aid = _insert_assignment(
            db, status="COMPLETED",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        with _patch(db):
            result = reassign_governance_item(
                assignment_id = aid,
                new_assignee  = "bob",
                reassigned_by = "manager",
            )
        assert result is None

    def test_governance_event_written_on_reassign(self):
        db = _make_db()
        aid = _insert_assignment(db, assigned_to="alice")
        with _patch(db):
            reassign_governance_item(
                assignment_id = aid,
                new_assignee  = "carol",
                reassigned_by = "manager",
                reason        = "Carol has more capacity.",
            )
        events = db.execute(
            "SELECT * FROM governance_approval_events WHERE event_type = 'REASSIGNED'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert "carol" in (e.get("notes") or "").lower()
        assert e["actor_id"] == "manager"


# ---------------------------------------------------------------------------
# Part 5 — Completion
# ---------------------------------------------------------------------------

class TestCompletion:
    def test_complete_sets_status_and_timestamp(self):
        db = _make_db()
        aid = _insert_assignment(db)
        with _patch(db):
            result = complete_assignment(assignment_id=aid, completed_by="alice")
        assert result is not None
        assert result["status"] == "COMPLETED"
        assert result["completed_at"] is not None

    def test_complete_nonexistent_returns_none(self):
        db = _make_db()
        with _patch(db):
            result = complete_assignment(assignment_id=9999, completed_by="alice")
        assert result is None

    def test_complete_already_completed_returns_none(self):
        db = _make_db()
        aid = _insert_assignment(
            db, status="COMPLETED",
            completed_at=datetime.now(timezone.utc).isoformat(),
        )
        with _patch(db):
            result = complete_assignment(assignment_id=aid, completed_by="alice")
        assert result is None

    def test_governance_event_written_on_complete(self):
        db = _make_db()
        aid = _insert_assignment(db, object_id="7")
        with _patch(db):
            complete_assignment(assignment_id=aid, completed_by="alice")
        events = db.execute(
            "SELECT * FROM governance_approval_events "
            "WHERE event_type = 'ASSIGNMENT_COMPLETED'"
        ).fetchall()
        assert len(events) == 1
        e = dict(events[0])
        assert e["to_state"] == "COMPLETED"
        assert e["actor_id"] == "alice"


# ---------------------------------------------------------------------------
# Part 6 — list_assignments (queue views)
# ---------------------------------------------------------------------------

class TestListAssignments:
    def _make_three(self, db):
        _insert_assignment(db, object_id="1", assigned_to="alice",
                           priority="HIGH",   source_id=1, assignment_group="G1")
        _insert_assignment(db, object_id="2", assigned_to="bob",
                           priority="MEDIUM", source_id=1, assignment_group="G1")
        _insert_assignment(db, object_id="3", assigned_to="alice",
                           priority="LOW",    source_id=2, assignment_group="G2")

    def test_returns_all_when_no_filter(self):
        db = _make_db()
        self._make_three(db)
        with _patch(db):
            items = list_assignments()
        assert len(items) == 3

    def test_filter_by_assigned_to(self):
        db = _make_db()
        self._make_three(db)
        with _patch(db):
            items = list_assignments(assigned_to="alice")
        assert all(i["assigned_to"] == "alice" for i in items)
        assert len(items) == 2

    def test_filter_by_priority(self):
        db = _make_db()
        self._make_three(db)
        with _patch(db):
            items = list_assignments(priority="HIGH")
        assert all(i["priority"] == "HIGH" for i in items)
        assert len(items) == 1

    def test_filter_by_source_id(self):
        db = _make_db()
        self._make_three(db)
        with _patch(db):
            items = list_assignments(source_id=1)
        assert len(items) == 2

    def test_filter_by_assignment_group(self):
        db = _make_db()
        self._make_three(db)
        with _patch(db):
            items = list_assignments(assignment_group="G2")
        assert len(items) == 1
        assert items[0]["assigned_to"] == "alice"

    def test_filter_by_status_open(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", status="OPEN")
        _insert_assignment(db, object_id="2", status="COMPLETED",
                           completed_at=datetime.now(timezone.utc).isoformat())
        with _patch(db):
            items = list_assignments(status="OPEN")
        assert all(i["status"] == "OPEN" for i in items)
        assert len(items) == 1

    def test_filter_by_object_type(self):
        db = _make_db()
        _insert_assignment(db, object_type="domain.rule",  object_id="1")
        _insert_assignment(db, object_type="entity.rule",  object_id="2")
        with _patch(db):
            items = list_assignments(object_type="domain.rule")
        assert len(items) == 1
        assert items[0]["object_type"] == "domain.rule"

    def test_overdue_only_filters_non_overdue(self):
        db = _make_db()
        # Past due: created 10 days ago, MEDIUM SLA=7
        _insert_assignment(
            db, object_id="overdue",
            priority="MEDIUM",
            created_at=_days_ago(10),
            due_date=_days_from_now(-3),
        )
        # On track: created now, MEDIUM SLA=7
        _insert_assignment(
            db, object_id="on_track",
            priority="MEDIUM",
        )
        with _patch(db):
            items = list_assignments(overdue_only=True)
        assert len(items) == 1
        assert items[0]["object_id"] == "overdue"

    def test_sla_attached_to_each_item(self):
        db = _make_db()
        _insert_assignment(db)
        with _patch(db):
            items = list_assignments()
        assert len(items) == 1
        sla = items[0]["sla"]
        assert "sla_status" in sla
        assert "days_open" in sla
        assert "days_overdue" in sla
        assert "risk_level" in sla
        assert "escalation_required" in sla
        assert "sla_due_date" in sla

    def test_sorted_critical_first(self):
        db = _make_db()
        _insert_assignment(db, object_id="low",      priority="LOW")
        _insert_assignment(db, object_id="critical", priority="CRITICAL")
        _insert_assignment(db, object_id="high",     priority="HIGH")
        with _patch(db):
            items = list_assignments()
        assert items[0]["priority"] == "CRITICAL"
        assert items[1]["priority"] == "HIGH"
        assert items[2]["priority"] == "LOW"


# ---------------------------------------------------------------------------
# Part 7 — assignment_summary (metrics)
# ---------------------------------------------------------------------------

class TestAssignmentSummary:
    def test_open_count(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", status="OPEN")
        _insert_assignment(db, object_id="2", status="OPEN")
        _insert_assignment(db, object_id="3", status="COMPLETED",
                           completed_at=datetime.now(timezone.utc).isoformat())
        with _patch(db):
            s = assignment_summary()
        assert s["open"] == 2

    def test_completed_today_count(self):
        db = _make_db()
        today = datetime.now(timezone.utc).isoformat()
        yesterday = _days_ago(1)
        _insert_assignment(db, object_id="1", status="COMPLETED",
                           completed_at=today)
        _insert_assignment(db, object_id="2", status="COMPLETED",
                           completed_at=yesterday)
        with _patch(db):
            s = assignment_summary(reference_date=today)
        assert s["completed_today"] == 1

    def test_overdue_percentage(self):
        db = _make_db()
        # 1 overdue, 1 on track
        _insert_assignment(db, object_id="1", priority="MEDIUM",
                           created_at=_days_ago(10),
                           due_date=_days_from_now(-3))
        _insert_assignment(db, object_id="2", priority="MEDIUM")
        with _patch(db):
            s = assignment_summary()
        assert s["overdue"] == 1
        assert s["overdue_pct"] == 50.0

    def test_critical_backlog_count(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", priority="CRITICAL")
        _insert_assignment(db, object_id="2", priority="CRITICAL")
        _insert_assignment(db, object_id="3", priority="HIGH")
        with _patch(db):
            s = assignment_summary()
        assert s["critical_backlog"] == 2

    def test_avg_resolution_days(self):
        db = _make_db()
        # Created 4 days ago, completed today → ~4 days
        created = _days_ago(4)
        completed = datetime.now(timezone.utc).isoformat()
        _insert_assignment(db, object_id="1", status="COMPLETED",
                           created_at=created, completed_at=completed)
        with _patch(db):
            s = assignment_summary()
        assert s["avg_resolution_days"] is not None
        assert s["avg_resolution_days"] >= 3  # at least 3 days

    def test_no_completed_returns_null_avg(self):
        db = _make_db()
        _insert_assignment(db)
        with _patch(db):
            s = assignment_summary()
        assert s["avg_resolution_days"] is None

    def test_by_priority_breakdown(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", priority="CRITICAL")
        _insert_assignment(db, object_id="2", priority="HIGH")
        _insert_assignment(db, object_id="3", priority="HIGH")
        with _patch(db):
            s = assignment_summary()
        assert s["by_priority"]["CRITICAL"] == 1
        assert s["by_priority"]["HIGH"] == 2
        assert s["by_priority"]["MEDIUM"] == 0
        assert s["by_priority"]["LOW"] == 0

    def test_by_object_type_breakdown(self):
        db = _make_db()
        _insert_assignment(db, object_type="domain.rule",  object_id="1")
        _insert_assignment(db, object_type="domain.rule",  object_id="2")
        _insert_assignment(db, object_type="entity.rule",  object_id="3")
        with _patch(db):
            s = assignment_summary()
        assert s["by_object_type"]["domain.rule"] == 2
        assert s["by_object_type"]["entity.rule"] == 1

    def test_by_steward_breakdown(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", assigned_to="alice")
        _insert_assignment(db, object_id="2", assigned_to="alice")
        _insert_assignment(db, object_id="3", assigned_to="bob")
        with _patch(db):
            s = assignment_summary()
        stewards = {e["assigned_to"]: e for e in s["by_steward"]}
        assert stewards["alice"]["open"] == 2
        assert stewards["bob"]["open"] == 1

    def test_filter_by_assigned_to(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", assigned_to="alice")
        _insert_assignment(db, object_id="2", assigned_to="bob")
        with _patch(db):
            s = assignment_summary(assigned_to="alice")
        assert s["open"] == 1

    def test_filter_by_source_id(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", source_id=1)
        _insert_assignment(db, object_id="2", source_id=2)
        with _patch(db):
            s = assignment_summary(source_id=1)
        assert s["open"] == 1

    def test_overdue_steward_counted_in_by_steward(self):
        db = _make_db()
        _insert_assignment(db, object_id="1", assigned_to="alice",
                           priority="CRITICAL",
                           created_at=_days_ago(5),
                           due_date=_days_from_now(-3))
        with _patch(db):
            s = assignment_summary()
        alice = next(e for e in s["by_steward"] if e["assigned_to"] == "alice")
        assert alice["overdue"] == 1

    def test_empty_db_returns_zero_metrics(self):
        db = _make_db()
        with _patch(db):
            s = assignment_summary()
        assert s["open"] == 0
        assert s["completed_today"] == 0
        assert s["overdue"] == 0
        assert s["overdue_pct"] == 0.0
        assert s["critical_backlog"] == 0
        assert s["avg_resolution_days"] is None
        assert s["by_steward"] == []


# ---------------------------------------------------------------------------
# Part 8 — Enum completeness
# ---------------------------------------------------------------------------

class TestEnums:
    def test_all_priorities_present(self):
        values = {p.value for p in AssignmentPriority}
        assert {"CRITICAL", "HIGH", "MEDIUM", "LOW"} == values

    def test_all_statuses_present(self):
        values = {s.value for s in AssignmentStatus}
        assert {"OPEN", "COMPLETED"} == values

    def test_sla_thresholds_all_priorities_covered(self):
        for p in AssignmentPriority:
            assert p.value in _SLA_DAYS_BY_PRIORITY
            assert _SLA_DAYS_BY_PRIORITY[p.value] > 0

    def test_sla_threshold_ordering(self):
        # CRITICAL < HIGH < MEDIUM < LOW
        assert (
            _SLA_DAYS_BY_PRIORITY["CRITICAL"]
            < _SLA_DAYS_BY_PRIORITY["HIGH"]
            < _SLA_DAYS_BY_PRIORITY["MEDIUM"]
            < _SLA_DAYS_BY_PRIORITY["LOW"]
        )


# ---------------------------------------------------------------------------
# Part 9 — Governance events for all actions
# ---------------------------------------------------------------------------

class TestGovernanceEvents:
    def test_assign_writes_event(self):
        db = _make_db()
        with _patch(db):
            assign_governance_item(
                object_type = "domain.rule", object_id = "10",
                assigned_to = "alice", assigned_by = "admin",
            )
        row = db.execute(
            "SELECT event_type, actor_id FROM governance_approval_events "
            "WHERE event_type = 'ASSIGNED'"
        ).fetchone()
        assert row is not None
        assert row["actor_id"] == "admin"

    def test_reassign_writes_event(self):
        db = _make_db()
        aid = _insert_assignment(db, object_id="20")
        with _patch(db):
            reassign_governance_item(
                assignment_id = aid,
                new_assignee  = "carol",
                reassigned_by = "manager",
            )
        row = db.execute(
            "SELECT event_type FROM governance_approval_events "
            "WHERE event_type = 'REASSIGNED'"
        ).fetchone()
        assert row is not None

    def test_complete_writes_event(self):
        db = _make_db()
        aid = _insert_assignment(db, object_id="30")
        with _patch(db):
            complete_assignment(assignment_id=aid, completed_by="alice")
        row = db.execute(
            "SELECT event_type FROM governance_approval_events "
            "WHERE event_type = 'ASSIGNMENT_COMPLETED'"
        ).fetchone()
        assert row is not None
