"""
Autonomous PII classification (Autonomous Live-SQL Foundation, Parts 2-4).

Part A: classify_column() is a pure function (no DB) — the adversarial cases
the milestone brief explicitly names: Email, FirstName, Phone, CourseFee,
ClassID, ambiguous Name, free-text Description, numeric identifier,
high-cardinality text.

Part B: run_pii_classification() against a real per-test SQLite DB (matches
tests/test_lifecycle_profiling_trigger.py's own fixture convention) —
idempotency and manual-override precedence.

Run from the project root:
    venv/Scripts/pytest tests/test_pii_classification_service.py -v
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-pii-classification-long-enough!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-pii-classification-value")

import data.models as models
from data.pii_classification_service import (
    CONFIRMED_NON_PII, CONFIRMED_PII, UNCERTAIN, classify_column, run_pii_classification,
)

_NOW = "2026-08-22T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Part A — pure function, adversarial cases from the milestone brief
# ---------------------------------------------------------------------------

def test_email_confirmed_pii():
    classification, confidence, _ = classify_column("Email", "TEXT")
    assert classification == CONFIRMED_PII
    assert confidence >= 0.9


def test_firstname_confirmed_pii():
    classification, _, _ = classify_column("FirstName", "TEXT")
    assert classification == CONFIRMED_PII


def test_phone_confirmed_pii():
    classification, _, _ = classify_column("Phone", "TEXT")
    assert classification == CONFIRMED_PII


def test_instructor_fname_compound_confirmed_pii():
    # Real the reference database case: PascalCase tokenizes to "instructor"+"fname", never a
    # literal "name" token — must hit Tier 1, not depend on Tier 3 at all.
    classification, _, _ = classify_column("InstructorFname", "TEXT")
    assert classification == CONFIRMED_PII


def test_course_fee_confirmed_non_pii():
    classification, _, _ = classify_column("CourseFee", "NUMERIC")
    assert classification == CONFIRMED_NON_PII


def test_class_id_confirmed_non_pii():
    classification, _, _ = classify_column("ClassID", "INTEGER")
    assert classification == CONFIRMED_NON_PII


def test_bare_name_is_uncertain_not_auto_confirmed():
    # Bare "Name" column — genuinely ambiguous (could be a person or a
    # business object); must fail conservatively, never auto-confirm either
    # direction.
    classification, confidence, _ = classify_column("Name", "TEXT")
    assert classification == UNCERTAIN
    assert confidence < 0.9


def test_business_object_name_confirmed_non_pii():
    # "TrackName"/"ClassName" — Tier 3's own documented false-positive class;
    # qualifier is a business object, not a person-referring word, so this
    # must not even reach UNCERTAIN.
    classification, _, _ = classify_column("TrackName", "TEXT")
    assert classification == CONFIRMED_NON_PII
    classification, _, _ = classify_column("ClassName", "TEXT")
    assert classification == CONFIRMED_NON_PII


def test_person_qualified_name_is_uncertain():
    # "StudentName" — Tier 3 person-qualifier hit, still ambiguous enough
    # (per pii_detector's own documented false-positive history) to stay
    # UNCERTAIN rather than auto-confirm.
    classification, _, _ = classify_column("StudentName", "TEXT")
    assert classification == UNCERTAIN


def test_free_text_description_confirmed_non_pii():
    classification, _, _ = classify_column("Description", "TEXT")
    assert classification == CONFIRMED_NON_PII


def test_numeric_identifier_confirmed_non_pii():
    classification, _, _ = classify_column("RecordID", "INTEGER")
    assert classification == CONFIRMED_NON_PII


def test_high_cardinality_non_name_text_confirmed_non_pii():
    classification, _, _ = classify_column("Notes", "TEXT")
    assert classification == CONFIRMED_NON_PII


def test_ssn_confirmed_pii():
    classification, confidence, _ = classify_column("SSN", "TEXT")
    assert classification == CONFIRMED_PII
    assert confidence >= 0.9


# ---------------------------------------------------------------------------
# Part B — run_pii_classification against a real DB: idempotency + manual
# override precedence
# ---------------------------------------------------------------------------

def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "pii_classification.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()

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
        "(id, source_id, snapshot_version, source_type, table_count, view_count, column_count, "
        " snapshot_json, discovered_at, created_at) VALUES (1, 1, 1, 'mssql', 1, 0, 3, '{}', ?, ?)",
        (_NOW, _NOW),
    )
    conn.execute(
        "INSERT INTO profiling_snapshots "
        "(id, source_id, schema_snapshot_id, snapshot_version, status, "
        " tables_total, next_table_index, created_at, completed_at) "
        "VALUES (1, 1, 1, 1, 'COMPLETE', 1, 1, ?, ?)",
        (_NOW, _NOW),
    )

    def _col(name: str, data_type: str, manual_override: int = 0) -> None:
        conn.execute(
            "INSERT INTO profiling_column_profiles "
            "(profiling_snapshot_id, source_id, table_fqn, column_name, data_type, "
            " pii_name_heuristic, pii_confirmed, pii_manual_override, created_at, updated_at) "
            "VALUES (1, 1, 'dbo.EDU_Student', ?, ?, 0, 0, ?, ?, ?)",
            (name, data_type, manual_override, _NOW, _NOW),
        )

    _col("Email", "TEXT")
    _col("FirstName", "TEXT")
    _col("CourseFee", "NUMERIC")
    _col("Name", "TEXT")
    conn.commit()
    conn.close()
    return db_path


def _cols(db_path: str) -> dict[str, sqlite3.Row]:
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM profiling_column_profiles WHERE source_id = 1"
    ).fetchall()
    conn.close()
    return {r["column_name"]: r for r in rows}


def test_run_pii_classification_sets_expected_states(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    summary = run_pii_classification(1)
    assert summary.columns_evaluated == 4
    assert summary.confirmed_pii == 2       # Email, FirstName
    assert summary.confirmed_non_pii == 1   # CourseFee
    assert summary.uncertain == 1           # Name

    cols = _cols(db_path)
    assert cols["Email"]["pii_confirmed"] == 1
    assert cols["Email"]["pii_classification"] == CONFIRMED_PII
    assert cols["FirstName"]["pii_confirmed"] == 1
    assert cols["CourseFee"]["pii_confirmed"] == 0
    assert cols["CourseFee"]["pii_classification"] == CONFIRMED_NON_PII
    assert cols["Name"]["pii_confirmed"] == 0
    assert cols["Name"]["pii_classification"] == UNCERTAIN


def test_run_pii_classification_is_idempotent(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    run_pii_classification(1)
    first = _cols(db_path)
    run_pii_classification(1)
    second = _cols(db_path)
    for name in first:
        assert first[name]["pii_classification"] == second[name]["pii_classification"]
        assert first[name]["pii_confirmed"] == second[name]["pii_confirmed"]


def test_manual_override_survives_reclassification(tmp_path, monkeypatch):
    db_path = _env(tmp_path, monkeypatch)
    # Simulate a human having manually cleared "Name" as non-PII before
    # autonomous classification ever runs.
    conn = _db_conn(db_path)
    conn.execute(
        "UPDATE profiling_column_profiles SET pii_manual_override = 1, "
        "pii_classification = 'CONFIRMED_NON_PII', pii_confirmed = 0 "
        "WHERE source_id = 1 AND column_name = 'Name'"
    )
    conn.commit()
    conn.close()

    summary = run_pii_classification(1)
    assert summary.manual_override_skipped == 1
    assert summary.columns_evaluated == 4
    # The other 3 (non-overridden) columns were still classified normally.
    assert summary.confirmed_pii == 2
    assert summary.confirmed_non_pii == 1

    cols = _cols(db_path)
    assert cols["Name"]["pii_classification"] == "CONFIRMED_NON_PII"
    assert cols["Name"]["pii_manual_override"] == 1
