"""
Portable Business Learning Layer milestone — unit tests for
data.schema_fingerprint_service and data.portable_knowledge_service.

Built on the real production schema (data.models.init_db) against a
per-test temp SQLite file, following the pattern established by
test_phase9_query_planning.py.
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-portable-knowledge-layer-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-portable-knowledge-layer-salt-long-enough-12345")

import data.models as models
from data.schema_fingerprint_service import compute_fingerprint, compare_fingerprints
import data.portable_knowledge_service as pks

_NOW = "2026-06-30T00:00:00+00:00"

_PATCHED_MODULES = (
    "data.schema_service",
    "data.portable_knowledge_service",
    "data.vocabulary_bootstrap_service",
    "data.entity_service",
    "data.semantic_contract_service",
)


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _snapshot_json(tables: list[dict]) -> str:
    return json.dumps({
        "source_id": 1, "source_type": "mssql", "discovered_at": _NOW,
        "schemas": [{"schema_name": "dbo", "tables": tables}],
        "database_name": "TestDB", "server_name": None, "connector_version": None,
        "discovery_duration_ms": 10, "warnings": [], "discovery_succeeded": True,
    })


def _table(name: str, columns: list[dict], pks_: list[str] | None = None, fks: list[dict] | None = None) -> dict:
    return {
        "table_name": name, "schema_name": "dbo", "table_fqn": f"dbo.{name}",
        "table_type": "TABLE", "row_count_estimate": 10,
        "columns": [
            {
                "column_name": c["name"], "ordinal_position": i, "data_type": c["type"],
                "raw_type": c["type"].lower(), "is_nullable": c.get("nullable", True),
                "is_primary_key": c.get("pk", False), "is_identity": c.get("pk", False),
                "max_length": None, "precision": None, "scale": None, "default_value": None,
            }
            for i, c in enumerate(columns)
        ],
        "primary_keys": [{"column_name": pk, "key_ordinal": i} for i, pk in enumerate(pks_ or [])],
        "foreign_keys": [
            {
                "fk_name": f"fk_{name}_{i}", "from_column": fk["from_column"],
                "to_schema": "dbo", "to_table": fk["to_table"], "to_column": fk["to_column"],
            }
            for i, fk in enumerate(fks or [])
        ],
    }


def env(tmp_path, monkeypatch, *, source_id: int = 1):
    db_path = str(tmp_path / "portable_knowledge.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    for mod in _PATCHED_MODULES:
        monkeypatch.setattr(f"{mod}.get_connection", lambda p=db_path: _db_conn(p))
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (?,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (source_id, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _write_snapshot(db_path: str, source_id: int, tables: list[dict]) -> None:
    conn = _db_conn(db_path)
    next_version = conn.execute(
        "SELECT COALESCE(MAX(snapshot_version), 0) + 1 FROM schema_snapshots WHERE source_id = ?",
        (source_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(source_id, snapshot_version, source_type, table_count, view_count, column_count, "
        " snapshot_json, discovered_at, created_at) VALUES (?,?,'mssql',?,0,0,?,?,?)",
        (source_id, next_version, len(tables), _snapshot_json(tables), _NOW, _NOW),
    )
    conn.commit()
    conn.close()


_STUDENT_TABLE = _table(
    "Student",
    [{"name": "StudentID", "type": "INTEGER", "pk": True}, {"name": "Name", "type": "TEXT"},
     {"name": "Status", "type": "TEXT"}],
    pks_=["StudentID"],
)
_CLIENT_TABLE = _table(
    "Clients",
    [{"name": "ClientID", "type": "INTEGER", "pk": True}, {"name": "Name", "type": "TEXT"}],
    pks_=["ClientID"],
)


# ---------------------------------------------------------------------------
# schema_fingerprint_service
# ---------------------------------------------------------------------------

def test_compute_fingerprint_returns_none_without_a_snapshot(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert compute_fingerprint(1, "u1") is None


def test_fingerprint_is_deterministic_regardless_of_table_or_column_order(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE, _CLIENT_TABLE])
    fp1 = compute_fingerprint(1, "u1")

    reordered_student = dict(_STUDENT_TABLE)
    reordered_student["columns"] = list(reversed(_STUDENT_TABLE["columns"]))
    _write_snapshot(db_path, 1, [_CLIENT_TABLE, reordered_student])
    fp2 = compute_fingerprint(1, "u1")

    assert fp1["fingerprint_hash"] == fp2["fingerprint_hash"]
    assert fp1["table_set_hash"] == fp2["table_set_hash"]


def test_fingerprint_changes_when_a_column_is_added(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    fp1 = compute_fingerprint(1, "u1")

    grown = dict(_STUDENT_TABLE)
    grown["columns"] = _STUDENT_TABLE["columns"] + [
        {"column_name": "Email", "ordinal_position": 3, "data_type": "TEXT", "raw_type": "varchar",
         "is_nullable": True, "is_primary_key": False, "is_identity": False,
         "max_length": None, "precision": None, "scale": None, "default_value": None}
    ]
    _write_snapshot(db_path, 1, [grown])
    fp2 = compute_fingerprint(1, "u1")

    assert fp1["fingerprint_hash"] != fp2["fingerprint_hash"]
    # Table set (names only) is unaffected by a column-level change.
    assert fp1["table_set_hash"] == fp2["table_set_hash"]


def test_compare_fingerprints_exact_match():
    a = {"fingerprint_version": 1, "fingerprint_hash": "h1", "table_set_hash": "t1",
         "table_fqns": ["dbo.Student"], "structural_summary": {"dbo.Student": {}}}
    result = compare_fingerprints(a, a)
    assert result["match"] == "EXACT"
    assert result["table_overlap_ratio"] == 1.0


def test_compare_fingerprints_no_match_for_unrelated_schema():
    package_fp = {"fingerprint_version": 1, "fingerprint_hash": "h1", "table_set_hash": "t1",
                  "table_fqns": ["dbo.Student", "dbo.Course"], "structural_summary": {}}
    live_fp = {"fingerprint_version": 1, "fingerprint_hash": "h2", "table_set_hash": "t2",
               "table_fqns": ["dbo.Patient", "dbo.Visit"], "structural_summary": {}}
    result = compare_fingerprints(package_fp, live_fp)
    assert result["match"] == "NO_MATCH"
    assert result["table_overlap_ratio"] == 0.0


def test_compare_fingerprints_compatible_drifted_when_majority_of_tables_survive():
    package_fp = {
        "fingerprint_version": 1, "fingerprint_hash": "h1", "table_set_hash": "t1",
        "table_fqns": ["dbo.Student", "dbo.Course", "dbo.Enrollment"],
        "structural_summary": {"dbo.Student": {"columns": []}, "dbo.Course": {"columns": []},
                                "dbo.Enrollment": {"columns": []}},
    }
    live_fp = {
        "fingerprint_version": 1, "fingerprint_hash": "h2", "table_set_hash": "t2",
        "table_fqns": ["dbo.Student", "dbo.Course"],
        "structural_summary": {"dbo.Student": {"columns": []}, "dbo.Course": {"columns": ["new"]}},
    }
    result = compare_fingerprints(package_fp, live_fp)
    assert result["match"] == "COMPATIBLE_DRIFTED"
    assert result["tables_removed"] == ["dbo.Enrollment"]


def test_compare_fingerprints_never_raises_on_malformed_input():
    assert compare_fingerprints({}, {})["match"] == "NO_MATCH"
    assert compare_fingerprints(None, None)["match"] == "NO_MATCH"


# ---------------------------------------------------------------------------
# portable_knowledge_service — sanitize / security audit
# ---------------------------------------------------------------------------

def test_sanitize_package_drops_keys_outside_the_allow_list():
    dirty = {
        "knowledge_package_version": 1, "fingerprint_version": 1,
        "toolsmith_compatibility_version": "1.0", "database_family_identifier": "x",
        "generated_at": _NOW, "human_reviewed": False, "provenance_note": "",
        "schema_fingerprint": {"fingerprint_version": 1, "fingerprint_hash": "h", "table_set_hash": "t",
                                "table_fqns": [], "structural_summary": {}},
        "vocabulary": [{"term": "student", "table_fqn": "dbo.Student", "column_name": "",
                         "confidence_tier": "HIGH", "confidence_score": 0.9, "evidence": [],
                         "required_schema_objects": ["dbo.Student"],
                         "encrypted_config_json": "SECRET_SHOULD_BE_DROPPED"}],
        "entity_assignments": [], "entity_contracts": [], "trusted_relationships": [],
        "connection_credentials": {"password": "hunter2"},
    }
    clean = pks.sanitize_package(dirty)
    assert "connection_credentials" not in clean
    assert "encrypted_config_json" not in clean["vocabulary"][0]
    assert clean["vocabulary"][0]["term"] == "student"


def test_audit_package_for_unsafe_content_flags_forbidden_key():
    package = {"database_family_identifier": "x", "connection": {"password": "hunter2"}}
    violations = pks.audit_package_for_unsafe_content(package)
    assert violations


def test_audit_package_for_unsafe_content_does_not_flag_a_column_literally_named_password():
    """Live-confirmed against a real production database: dbo.EDU_Channels has a column
    literally named "ApiKey" and another table has "In-Password" — both are
    legitimate structural metadata (column NAMES, never VALUES) and must
    never be flagged as a leaked secret."""
    package = {
        "database_family_identifier": "x",
        "schema_fingerprint": {
            "fingerprint_version": 1, "fingerprint_hash": "h", "table_set_hash": "t",
            "table_fqns": ["dbo.EDU_Channels"],
            "structural_summary": {
                "dbo.EDU_Channels": {"columns": [
                    {"column_name": "ApiKey", "data_type": "TEXT", "is_nullable": False, "is_primary_key": False},
                ], "primary_keys": [], "foreign_keys": []},
            },
        },
    }
    assert pks.audit_package_for_unsafe_content(package) == []


def test_audit_package_for_unsafe_content_clean_package_has_no_violations(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    package = pks.build_package_from_source(1, "u1", "test_family_v1", min_confidence=0.0)
    assert package is not None
    assert pks.audit_package_for_unsafe_content(package) == []


# ---------------------------------------------------------------------------
# portable_knowledge_service — build / validate / activate round trip
# ---------------------------------------------------------------------------

def _insert_vocabulary_row(db_path: str, source_id: int, term: str, table_fqn: str, confidence: float) -> None:
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO generated_business_vocabulary "
        "(source_id, schema_snapshot_id, term, table_fqn, column_name, confidence_tier, "
        " confidence_score, evidence_json, created_at, updated_at) "
        "VALUES (?, 1, ?, ?, '', 'HIGH', ?, '[]', ?, ?)",
        (source_id, term, table_fqn, confidence, _NOW, _NOW),
    )
    conn.commit()
    conn.close()


def test_build_package_only_includes_rows_at_or_above_min_confidence(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_path, 1, "student", "dbo.Student", 0.9)
    _insert_vocabulary_row(db_path, 1, "pupil", "dbo.Student", 0.2)

    package = pks.build_package_from_source(1, "u1", "test_family_v1", min_confidence=0.6)
    terms = {row["term"] for row in package["vocabulary"]}
    assert terms == {"student"}


def test_build_package_returns_none_for_unowned_source(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert pks.build_package_from_source(1, "wrong-user", "test_family_v1") is None


def test_validate_and_activate_materializes_valid_rows_into_a_new_source(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch, source_id=1)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    _insert_vocabulary_row(db_path, 1, "student", "dbo.Student", 0.9)
    package = pks.build_package_from_source(1, "u1", "test_family_v1", min_confidence=0.6)
    assert len(package["vocabulary"]) == 1

    # Simulate a fresh reconnect: a brand-new source_id=2, same physical
    # schema, zero prior vocabulary of its own.
    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Test2','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    _write_snapshot(db_path, 2, [_STUDENT_TABLE])

    validation = pks.validate_package_against_live_schema(package, 2, "u1")
    assert validation["match"] == "EXACT"
    vocab_statuses = [c["status"] for c in validation["sections"]["vocabulary"]]
    assert vocab_statuses == ["VALID"]

    counts = pks.activate_package(package, validation, 2, "u1")
    assert counts["vocabulary"]["activated"] == 1

    import data.vocabulary_bootstrap_service as vocab_svc
    activated = vocab_svc.get_generated_vocabulary(2)
    assert "student" in activated
    assert activated["student"][0]["table_fqn"] == "dbo.Student"


def test_validate_rejects_assertion_whose_table_no_longer_exists(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch, source_id=1)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE, _CLIENT_TABLE])
    _insert_vocabulary_row(db_path, 1, "client", "dbo.Clients", 0.9)
    package = pks.build_package_from_source(1, "u1", "test_family_v1", min_confidence=0.6)

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (2,'u1','Test2','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    conn.commit()
    conn.close()
    # Clients table dropped on the "reconnected" schema.
    _write_snapshot(db_path, 2, [_STUDENT_TABLE])

    validation = pks.validate_package_against_live_schema(package, 2, "u1")
    assert validation["match"] == "COMPATIBLE_DRIFTED"
    statuses = [c["status"] for c in validation["sections"]["vocabulary"]]
    assert statuses == ["INCOMPATIBLE"]

    counts = pks.activate_package(package, validation, 2, "u1")
    assert counts["vocabulary"]["activated"] == 0
    assert counts["vocabulary"]["INCOMPATIBLE"] == 1


def test_find_matching_package_prefers_exact_over_compatible(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch, source_id=1)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    exact_fp = compute_fingerprint(1, "u1")

    drifted_fp = dict(exact_fp)
    drifted_fp["fingerprint_hash"] = "different"

    exact_package = {"database_family_identifier": "exact_family", "schema_fingerprint": exact_fp,
                      "vocabulary": [], "entity_assignments": [], "entity_contracts": [],
                      "trusted_relationships": []}
    drifted_package = {"database_family_identifier": "drifted_family", "schema_fingerprint": drifted_fp,
                        "vocabulary": [], "entity_assignments": [], "entity_contracts": [],
                        "trusted_relationships": []}

    match = pks.find_matching_package(1, "u1", packages=[drifted_package, exact_package])
    assert match["package"]["database_family_identifier"] == "exact_family"
    assert match["comparison"]["match"] == "EXACT"


def test_find_matching_package_returns_none_when_nothing_matches(tmp_path, monkeypatch):
    db_path = env(tmp_path, monkeypatch, source_id=1)
    _write_snapshot(db_path, 1, [_STUDENT_TABLE])
    unrelated_fp = {"fingerprint_version": 1, "fingerprint_hash": "nope", "table_set_hash": "nope",
                     "table_fqns": ["dbo.Patient"], "structural_summary": {}}
    unrelated_package = {"database_family_identifier": "unrelated", "schema_fingerprint": unrelated_fp,
                          "vocabulary": [], "entity_assignments": [], "entity_contracts": [],
                          "trusted_relationships": []}
    assert pks.find_matching_package(1, "u1", packages=[unrelated_package]) is None
