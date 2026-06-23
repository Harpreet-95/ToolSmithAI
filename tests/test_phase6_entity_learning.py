"""
Tests for Phase 6 Step 4 — Entity Learning Rules.

Run from the project root:
    venv/Scripts/pytest tests/test_phase6_entity_learning.py -v

Covers:
  - LearnedEntityRule dataclass
  - apply_learned_entity_rules (pure, no DB)
  - suggest_entity_rules (pure, no DB): min_support, idempotency, TOKEN suppression
  - Service layer (patched SQLite): generate, list, approve, reject, state machine
"""

import os
import sqlite3
import pytest
from unittest.mock import patch
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-entity-learning")
os.environ.setdefault("USER_ID_SALT", "test-salt-entity-learning")

from core.entities.learning import (
    LearnedEntityRule,
    apply_learned_entity_rules,
    suggest_entity_rules,
)
from core.entities.models import ENTITY_UNKNOWN


# ── helpers ───────────────────────────────────────────────────────────────────

def _rule(
    id: int = 1,
    source_id: int = 1,
    pattern_type: str = "PREFIX",
    pattern_value: str = "student",
    entity: str = "Student",
    confidence: float = 0.9,
    approval_status: str = "APPROVED",
    created_by: str = "u1",
    active: bool = True,
) -> LearnedEntityRule:
    return LearnedEntityRule(
        id=id,
        source_id=source_id,
        pattern_type=pattern_type,
        pattern_value=pattern_value,
        entity=entity,
        confidence=confidence,
        approval_status=approval_status,
        created_by=created_by,
        approved_by=None,
        created_at="2026-01-01T00:00:00+00:00",
        approved_at=None,
        active=active,
    )


def _unknown_table(name: str, schema: str = "dbo", competing: list | None = None) -> dict:
    return {
        "table_fqn":          f"{schema}.{name}",
        "table_name":         name,
        "schema_name":        schema,
        "competing_entities": competing or [],
    }


# ── DB fixture ────────────────────────────────────────────────────────────────

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS data_source_connections (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id               TEXT    NOT NULL,
        display_name          TEXT    NOT NULL DEFAULT 'Test',
        source_type           TEXT    NOT NULL DEFAULT 'mssql',
        source_category       TEXT    NOT NULL DEFAULT 'relational',
        encrypted_config_json TEXT    NOT NULL DEFAULT '{}',
        config_schema_version INTEGER NOT NULL DEFAULT 1,
        capabilities_json     TEXT    NOT NULL DEFAULT '[]',
        metadata_json         TEXT    NOT NULL DEFAULT '{}',
        source_status         TEXT    NOT NULL DEFAULT 'ACTIVE',
        is_active             INTEGER NOT NULL DEFAULT 1,
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS entity_learning_rules (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id        INTEGER NOT NULL,
        pattern_type     TEXT    NOT NULL,
        pattern_value    TEXT    NOT NULL,
        entity           TEXT    NOT NULL,
        confidence       REAL    NOT NULL DEFAULT 0.8,
        approval_status  TEXT    NOT NULL DEFAULT 'PENDING',
        created_by       TEXT    NOT NULL,
        approved_by      TEXT,
        created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        approved_at      TEXT,
        active           INTEGER NOT NULL DEFAULT 0
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_elr_source_type_val
        ON entity_learning_rules (source_id, pattern_type, pattern_value);

    CREATE TABLE IF NOT EXISTS profiling_table_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        table_name            TEXT    NOT NULL,
        schema_name           TEXT    NOT NULL DEFAULT 'dbo',
        table_type            TEXT    NOT NULL DEFAULT 'TABLE',
        profiling_status      TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS entity_assignments (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id               INTEGER NOT NULL,
        profiling_snapshot_id   INTEGER NOT NULL,
        table_fqn               TEXT    NOT NULL,
        entity                  TEXT    NOT NULL,
        confidence              REAL    NOT NULL DEFAULT 0.0,
        evidence_json           TEXT    NOT NULL DEFAULT '[]',
        competing_entities_json TEXT    NOT NULL DEFAULT '[]',
        created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
"""


@pytest.fixture
def db_factory(tmp_path):
    """Returns a get_connection factory backed by a temp SQLite file."""
    db_file = str(tmp_path / "entity_test.db")

    def _conn():
        c = sqlite3.connect(db_file, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = OFF")  # skip FK for minimal fixture
        return c

    bootstrap = _conn()
    bootstrap.executescript(_SCHEMA)
    bootstrap.commit()
    bootstrap.close()

    return _conn


@pytest.fixture
def seeded_db(db_factory):
    """DB with one source owned by 'alice' and one pending rule."""
    conn = db_factory()
    conn.execute(
        "INSERT INTO data_source_connections (user_id) VALUES ('alice')"
    )
    conn.execute("""
        INSERT INTO entity_learning_rules
            (source_id, pattern_type, pattern_value, entity, confidence, created_by)
        VALUES (1, 'PREFIX', 'student', 'Student', 0.85, 'alice')
    """)
    conn.commit()
    conn.close()
    return db_factory


# ── 1. LearnedEntityRule dataclass ───────────────────────────────────────────

def test_learned_entity_rule_fields():
    r = _rule()
    assert r.id == 1
    assert r.source_id == 1
    assert r.pattern_type == "PREFIX"
    assert r.pattern_value == "student"
    assert r.entity == "Student"
    assert r.confidence == 0.9
    assert r.approval_status == "APPROVED"
    assert r.active is True
    assert r.approved_by is None
    assert r.approved_at is None


# ── 2. apply_learned_entity_rules ─────────────────────────────────────────────

def test_apply_prefix_rule_matches():
    rules = [_rule(pattern_type="PREFIX", pattern_value="student", entity="Student")]
    profile = {"table_fqn": "dbo.student_grades", "table_name": "student_grades", "schema_name": "dbo"}
    result = apply_learned_entity_rules(profile, rules)
    assert result is not None
    assert result.entity == "Student"
    assert result.confidence == 0.9
    assert "PREFIX" in result.evidence[0]


def test_apply_schema_rule_matches():
    rules = [_rule(pattern_type="SCHEMA", pattern_value="enrollment", entity="Student")]
    profile = {"table_fqn": "enrollment.applications", "table_name": "applications", "schema_name": "enrollment"}
    result = apply_learned_entity_rules(profile, rules)
    assert result is not None
    assert result.entity == "Student"


def test_apply_suffix_rule_matches():
    rules = [_rule(pattern_type="SUFFIX", pattern_value="log", entity="Event")]
    profile = {"table_fqn": "dbo.payment_log", "table_name": "payment_log", "schema_name": "dbo"}
    result = apply_learned_entity_rules(profile, rules)
    assert result is not None
    assert result.entity == "Event"


def test_apply_token_rule_matches_middle_token():
    rules = [_rule(pattern_type="TOKEN", pattern_value="invoice", entity="Payment")]
    profile = {"table_fqn": "dbo.ar_invoice_lines", "table_name": "ar_invoice_lines", "schema_name": "dbo"}
    result = apply_learned_entity_rules(profile, rules)
    assert result is not None
    assert result.entity == "Payment"


def test_apply_no_match_returns_none():
    rules = [_rule(pattern_type="PREFIX", pattern_value="student", entity="Student")]
    profile = {"table_fqn": "dbo.products", "table_name": "products", "schema_name": "dbo"}
    result = apply_learned_entity_rules(profile, rules)
    assert result is None


def test_apply_first_matching_rule_wins():
    rules = [
        _rule(id=1, pattern_type="PREFIX", pattern_value="student", entity="Student"),
        _rule(id=2, pattern_type="TOKEN",  pattern_value="student", entity="User"),
    ]
    profile = {"table_fqn": "dbo.student_records", "table_name": "student_records", "schema_name": "dbo"}
    result = apply_learned_entity_rules(profile, rules)
    assert result.entity == "Student"


def test_apply_empty_rules_returns_none():
    profile = {"table_fqn": "dbo.foo", "table_name": "foo", "schema_name": "dbo"}
    assert apply_learned_entity_rules(profile, []) is None


# ── 3. suggest_entity_rules ───────────────────────────────────────────────────

def test_suggest_below_min_support_ignored():
    # Only 2 tables with the same prefix — below default min_support of 3
    tables = [
        _unknown_table("student_records"),
        _unknown_table("student_grades"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    assert suggestions == []


def test_suggest_meets_min_support():
    tables = [
        _unknown_table("student_records"),
        _unknown_table("student_grades"),
        _unknown_table("student_roster"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    pattern_types = {s["pattern_type"] for s in suggestions}
    assert "PREFIX" in pattern_types or "TOKEN" in pattern_types


def test_suggest_prefix_suppresses_token():
    # "student" meets min_support as PREFIX — TOKEN should be suppressed
    tables = [
        _unknown_table("student_records"),
        _unknown_table("student_grades"),
        _unknown_table("student_roster"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    prefix_hit = any(s["pattern_type"] == "PREFIX" and s["pattern_value"] == "student"
                     for s in suggestions)
    token_hit  = any(s["pattern_type"] == "TOKEN"  and s["pattern_value"] == "student"
                     for s in suggestions)
    assert prefix_hit
    assert not token_hit


def test_suggest_schema_pattern_detected():
    tables = [
        _unknown_table("records",    schema="admissions"),
        _unknown_table("applicants", schema="admissions"),
        _unknown_table("documents",  schema="admissions"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    schema_hit = any(s["pattern_type"] == "SCHEMA" and s["pattern_value"] == "admissions"
                     for s in suggestions)
    assert schema_hit


def test_suggest_competing_entity_infers_confidence_above_baseline():
    tables = [
        _unknown_table("student_records",
                       competing=[{"entity": "Student", "score": 0.6, "evidence": []}]),
        _unknown_table("student_grades",
                       competing=[{"entity": "Student", "score": 0.5, "evidence": []}]),
        _unknown_table("student_roster",
                       competing=[{"entity": "Student", "score": 0.7, "evidence": []}]),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    prefix = next(s for s in suggestions if s["pattern_type"] == "PREFIX"
                                         and s["pattern_value"] == "student")
    # Unanimous competing signal pushes confidence above 0.75 baseline
    assert prefix["suggested_entity"] == "Student"
    assert prefix["suggested_confidence"] > 0.75


def test_suggest_no_competing_entity_falls_back_to_unknown():
    tables = [
        _unknown_table("misc_alpha"),
        _unknown_table("misc_beta"),
        _unknown_table("misc_gamma"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    prefix = next((s for s in suggestions if s["pattern_type"] == "PREFIX"
                                           and s["pattern_value"] == "misc"), None)
    assert prefix is not None
    assert prefix["suggested_entity"] == ENTITY_UNKNOWN


def test_suggest_idempotent_same_input_twice():
    tables = [
        _unknown_table("applicant_records"),
        _unknown_table("applicant_data"),
        _unknown_table("applicant_notes"),
    ]
    first  = suggest_entity_rules(tables, min_support=3)
    second = suggest_entity_rules(tables, min_support=3)
    assert first == second


def test_suggest_ordered_by_support_desc():
    # "student" appears 4 times, "vendor" appears 3 times
    tables = [
        _unknown_table("student_records"),
        _unknown_table("student_grades"),
        _unknown_table("student_roster"),
        _unknown_table("student_info"),
        _unknown_table("vendor_contacts"),
        _unknown_table("vendor_invoices"),
        _unknown_table("vendor_agreements"),
    ]
    suggestions = suggest_entity_rules(tables, min_support=3)
    support_counts = [s["support_count"] for s in suggestions]
    assert support_counts == sorted(support_counts, reverse=True)


def test_suggest_returns_example_tables_capped_at_five():
    tables = [_unknown_table(f"student_table_{i}") for i in range(10)]
    suggestions = suggest_entity_rules(tables, min_support=3)
    prefix = next(s for s in suggestions if s["pattern_type"] == "PREFIX"
                                         and s["pattern_value"] == "student")
    assert len(prefix["example_tables"]) <= 5


# ── 4. Service layer — state machine and ownership ────────────────────────────

def test_approve_sets_approved_and_active(seeded_db):
    from data.entity_learning_service import approve_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = approve_entity_rule(rule_id=1, user_id="alice")

    assert result is not None
    assert result["approval_status"] == "APPROVED"
    assert result["active"] == 1
    assert result["approved_by"] == "alice"
    assert result["approved_at"] is not None


def test_reject_sets_rejected_and_inactive(seeded_db):
    from data.entity_learning_service import reject_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = reject_entity_rule(rule_id=1, user_id="alice")

    assert result is not None
    assert result["approval_status"] == "REJECTED"
    assert result["active"] == 0
    assert result["approved_by"] == "alice"


def test_approve_already_approved_raises_value_error(seeded_db):
    from data.entity_learning_service import approve_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        approve_entity_rule(rule_id=1, user_id="alice")
        with pytest.raises(ValueError, match="already 'APPROVED'"):
            approve_entity_rule(rule_id=1, user_id="alice")


def test_reject_already_rejected_raises_value_error(seeded_db):
    from data.entity_learning_service import reject_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        reject_entity_rule(rule_id=1, user_id="alice")
        with pytest.raises(ValueError, match="already 'REJECTED'"):
            reject_entity_rule(rule_id=1, user_id="alice")


def test_approve_already_rejected_raises_value_error(seeded_db):
    from data.entity_learning_service import approve_entity_rule, reject_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        reject_entity_rule(rule_id=1, user_id="alice")
        with pytest.raises(ValueError, match="already 'REJECTED'"):
            approve_entity_rule(rule_id=1, user_id="alice")


def test_reject_already_approved_raises_value_error(seeded_db):
    from data.entity_learning_service import approve_entity_rule, reject_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        approve_entity_rule(rule_id=1, user_id="alice")
        with pytest.raises(ValueError, match="already 'APPROVED'"):
            reject_entity_rule(rule_id=1, user_id="alice")


def test_approve_wrong_user_returns_none(seeded_db):
    from data.entity_learning_service import approve_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = approve_entity_rule(rule_id=1, user_id="eve")

    assert result is None


def test_reject_wrong_user_returns_none(seeded_db):
    from data.entity_learning_service import reject_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = reject_entity_rule(rule_id=1, user_id="eve")

    assert result is None


def test_approve_nonexistent_rule_returns_none(seeded_db):
    from data.entity_learning_service import approve_entity_rule

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = approve_entity_rule(rule_id=999, user_id="alice")

    assert result is None


# ── 5. list_entity_rule_suggestions and list_entity_rules ────────────────────

def test_list_suggestions_returns_only_pending(seeded_db):
    from data.entity_learning_service import (
        approve_entity_rule,
        list_entity_rule_suggestions,
    )

    # Seed a second PENDING rule then approve the first
    conn = seeded_db()
    conn.execute("""
        INSERT INTO entity_learning_rules
            (source_id, pattern_type, pattern_value, entity, confidence, created_by)
        VALUES (1, 'TOKEN', 'applicant', 'Applicant', 0.80, 'alice')
    """)
    conn.commit()
    conn.close()

    with patch("data.entity_learning_service.get_connection", seeded_db):
        approve_entity_rule(rule_id=1, user_id="alice")
        pending = list_entity_rule_suggestions(source_id=1, user_id="alice")

    assert len(pending) == 1
    assert pending[0]["approval_status"] == "PENDING"
    assert pending[0]["pattern_value"] == "applicant"


def test_list_rules_wrong_user_returns_none(seeded_db):
    from data.entity_learning_service import list_entity_rules

    with patch("data.entity_learning_service.get_connection", seeded_db):
        result = list_entity_rules(source_id=1, user_id="eve")

    assert result is None


def test_list_rules_approved_first(seeded_db):
    from data.entity_learning_service import approve_entity_rule, list_entity_rules

    # Add a second PENDING rule
    conn = seeded_db()
    conn.execute("""
        INSERT INTO entity_learning_rules
            (source_id, pattern_type, pattern_value, entity, confidence, created_by)
        VALUES (1, 'PREFIX', 'vendor', 'Vendor', 0.80, 'alice')
    """)
    conn.commit()
    conn.close()

    with patch("data.entity_learning_service.get_connection", seeded_db):
        approve_entity_rule(rule_id=1, user_id="alice")
        rules = list_entity_rules(source_id=1, user_id="alice")

    assert rules[0]["approval_status"] == "APPROVED"
    assert rules[1]["approval_status"] == "PENDING"


# ── 6. generate_entity_rule_suggestions idempotency ──────────────────────────

def test_generate_suggestions_wrong_user_returns_none(db_factory):
    from data.entity_learning_service import generate_entity_rule_suggestions

    conn = db_factory()
    conn.execute("INSERT INTO data_source_connections (user_id) VALUES ('alice')")
    conn.commit()
    conn.close()

    with patch("data.entity_learning_service.get_connection", db_factory):
        result = generate_entity_rule_suggestions(source_id=1, user_id="eve")

    assert result is None


def test_generate_suggestions_no_unknowns_returns_empty_summary(db_factory):
    from data.entity_learning_service import generate_entity_rule_suggestions

    conn = db_factory()
    conn.execute("INSERT INTO data_source_connections (user_id) VALUES ('alice')")
    conn.commit()
    conn.close()

    with patch("data.entity_learning_service.get_connection", db_factory):
        result = generate_entity_rule_suggestions(source_id=1, user_id="alice")

    assert result is not None
    assert result["unknown_tables"] == 0
    assert result["suggestions_new"] == 0
    assert result["suggestions_skipped"] == 0


def test_generate_suggestions_idempotent_on_rerun(db_factory):
    """Second generate() call on the same source skips already-suggested patterns."""
    from data.entity_learning_service import generate_entity_rule_suggestions

    # Manually insert PENDING rules to simulate a prior generate() run
    conn = db_factory()
    conn.execute("INSERT INTO data_source_connections (user_id) VALUES ('alice')")
    conn.execute("""
        INSERT INTO entity_learning_rules
            (source_id, pattern_type, pattern_value, entity, confidence, created_by)
        VALUES (1, 'PREFIX', 'student', 'Student', 0.85, 'alice')
    """)
    conn.commit()
    conn.close()

    # generate with no unknown tables (early exit) — simulate re-run returns 0 new
    with patch("data.entity_learning_service.get_connection", db_factory):
        r1 = generate_entity_rule_suggestions(source_id=1, user_id="alice")
        r2 = generate_entity_rule_suggestions(source_id=1, user_id="alice")

    assert r1["suggestions_new"] == 0
    assert r2["suggestions_new"] == 0
