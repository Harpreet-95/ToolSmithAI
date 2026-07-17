"""
Tests for Sprint 1.2 — retrieval ranking quality fixes in
data/search_service.py:

Fix 1: conservative singular/plural normalization so plural query terms
(students, courses, classes) match singular table names (ADF_Student,
ADF_Course, ADF_Class).

Fix 2: a non-authoritative naming penalty (test/temp/tmp/backup/bkup/
history/old/dated-copy) so a clean production table outranks its
non-authoritative namesakes without excluding them outright.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
test_search_unprofiled_tables.py.

Run from the project root:
    venv/Scripts/pytest tests/test_search_ranking_quality.py -v
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-search-ranking-quality-secret-long-eno")
os.environ.setdefault("USER_ID_SALT", "test-search-ranking-quality-salt-long-enou")

import data.models as models
from data.search_service import search_metadata, _singularize, _naming_penalty

_NOW = "2026-07-14T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _snapshot_json(*table_names):
    tables = [
        {
            "table_name": name, "schema_name": "dbo",
            "table_fqn": f"dbo.{name}", "table_type": "TABLE",
            "row_count_estimate": None, "columns": [], "primary_keys": [], "foreign_keys": [],
        }
        for name in table_names
    ]
    return json.dumps({
        "source_id": 1, "source_type": "mssql", "discovered_at": _NOW,
        "schemas": [{"schema_name": "dbo", "tables": tables}],
        "database_name": None, "server_name": None,
        "connector_version": None, "discovery_duration_ms": None, "warnings": [],
    })


def env(tmp_path, monkeypatch, *table_names):
    """Seed a source whose latest schema_snapshots row lists exactly
    *table_names — no profiling/dictionary rows, so ranking is driven purely
    by table_name (isolating Fix 1 / Fix 2 from other scoring signals)."""
    db_path = str(tmp_path / "ranking_quality.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
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
        " discovered_at, created_at) VALUES (1,1,1,'mssql',?,?,?,?)",
        (len(table_names), _snapshot_json(*table_names), _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _ranked_fqns(result):
    return [r["qualified_name"] for r in sorted(result["results"], key=lambda r: -r["relevance_score"])]


# ---------------------------------------------------------------------------
# Fix 1 — singular/plural normalization
# ---------------------------------------------------------------------------

def test_singularize_conservative_rules():
    assert _singularize("students") == "student"
    assert _singularize("courses") == "course"
    assert _singularize("classes") == "class"
    assert _singularize("companies") == "company"
    # Left alone: too short, or an -s ending unlikely to be a regular plural.
    assert _singularize("bus") == "bus"
    assert _singularize("status") == "status"
    assert _singularize("campus") == "campus"


def test_students_ranks_adf_student(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Student")
    result = search_metadata(q="students", source_id=1, asset_type="table")
    assert "dbo.ADF_Student" in _ranked_fqns(result)


def test_courses_ranks_adf_course(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Course")
    result = search_metadata(q="courses", source_id=1, asset_type="table")
    assert "dbo.ADF_Course" in _ranked_fqns(result)


def test_classes_ranks_adf_class(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Class")
    result = search_metadata(q="classes", source_id=1, asset_type="table")
    assert "dbo.ADF_Class" in _ranked_fqns(result)


def test_exact_literal_match_still_outranks_normalized_match(tmp_path, monkeypatch):
    # "student" (already singular) should score ADF_Student via the direct
    # substring/word-boundary tier, at least as high as the plural query
    # "students" scores it via the weaker normalized tier.
    env(tmp_path, monkeypatch, "ADF_Student")
    literal = search_metadata(q="student", source_id=1, asset_type="table")
    normalized = search_metadata(q="students", source_id=1, asset_type="table")
    literal_score = literal["results"][0]["relevance_score"]
    normalized_score = normalized["results"][0]["relevance_score"]
    assert literal_score >= normalized_score


# ---------------------------------------------------------------------------
# Fix 2 — non-authoritative naming penalty
# ---------------------------------------------------------------------------

def test_naming_penalty_detects_expected_patterns():
    assert _naming_penalty("ADF_Homework_test") > 0
    assert _naming_penalty("ADF_Homework_History") > 0
    assert _naming_penalty("adf_clients_temp") > 0
    assert _naming_penalty("CB_SUBSCRIPTION_PAYMENTS_backup_07062016") > 0
    assert _naming_penalty("CB_SUBSCRIPTION_PAYMENTS_1_17_2019") > 0
    assert _naming_penalty("ADF_Homework") == 0
    assert _naming_penalty("ADF_Clients") == 0


def test_adf_homework_ranks_above_test_and_history_variants(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Homework", "ADF_Homework_test", "ADF_Homework_History")
    result = search_metadata(q="homework", source_id=1, asset_type="table")
    by_fqn = {r["qualified_name"]: r["relevance_score"] for r in result["results"]}
    assert by_fqn["dbo.ADF_Homework"] > by_fqn["dbo.ADF_Homework_test"]
    assert by_fqn["dbo.ADF_Homework"] > by_fqn["dbo.ADF_Homework_History"]
    # Penalized, not excluded.
    assert "dbo.ADF_Homework_test" in by_fqn
    assert "dbo.ADF_Homework_History" in by_fqn


def test_adf_clients_ranks_above_temp_variant(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Clients", "adf_clients_temp")
    result = search_metadata(q="clients", source_id=1, asset_type="table")
    by_fqn = {r["qualified_name"]: r["relevance_score"] for r in result["results"]}
    assert by_fqn["dbo.ADF_Clients"] > by_fqn["dbo.adf_clients_temp"]
    assert "dbo.adf_clients_temp" in by_fqn


def test_subscription_payments_ranks_above_backup_and_dated_variants(tmp_path, monkeypatch):
    env(
        tmp_path, monkeypatch,
        "CB_SUBSCRIPTION_PAYMENTS",
        "CB_SUBSCRIPTION_PAYMENTS_backup_07062016",
        "CB_SUBSCRIPTION_PAYMENTS_1_17_2019",
    )
    result = search_metadata(q="subscription payments", source_id=1, asset_type="table")
    by_fqn = {r["qualified_name"]: r["relevance_score"] for r in result["results"]}
    clean = by_fqn["dbo.CB_SUBSCRIPTION_PAYMENTS"]
    assert clean > by_fqn["dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016"]
    assert clean > by_fqn["dbo.CB_SUBSCRIPTION_PAYMENTS_1_17_2019"]
    # Both variants still present — penalized, never removed.
    assert "dbo.CB_SUBSCRIPTION_PAYMENTS_backup_07062016" in by_fqn
    assert "dbo.CB_SUBSCRIPTION_PAYMENTS_1_17_2019" in by_fqn


def test_naming_penalty_reason_recorded_only_when_applied(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Homework", "ADF_Homework_test")
    result = search_metadata(q="homework", source_id=1, asset_type="table")
    by_fqn = {r["qualified_name"]: r["match_reasons"] for r in result["results"]}
    assert any("Naming penalty" in r for r in by_fqn["dbo.ADF_Homework_test"])
    assert not any("Naming penalty" in r for r in by_fqn["dbo.ADF_Homework"])


def test_response_contract_fields_unchanged(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch, "ADF_Homework_test")
    result = search_metadata(q="homework", source_id=1, asset_type="table")
    row = result["results"][0]
    for field in (
        "asset_type", "display_name", "qualified_name", "source_id", "source_name",
        "schema_name", "table_name", "relevance_score", "match_reasons", "domain",
        "entity", "dictionary_status", "pii_indicator", "semantic_type", "table_type",
        "confidence", "profiled_at", "profiling_status", "nav_target",
    ):
        assert field in row
