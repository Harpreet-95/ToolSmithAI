"""
Tests for the Structural PK-Name-Match Relationship Discovery
(Enterprise Implementation — Structural Relationship Inference).

Verifies discover_structural_pk_candidates() proposes PENDING relationship
candidates directly from schema_snapshot metadata (column names, normalized
data types, declared primary keys) with no dependency on profiling
statistics — the exact real-CCPP gap (ADF_Enrollment_Tracking.PathID/ClassID
never profiled in the latest profiling snapshot, so discover_relationship_
candidates() never saw them despite the columns matching declared PKs
exactly).

Follows the same real-schema fixture pattern as
test_phase7_relationship_intelligence.py (data.models.init_db against a
per-test temp SQLite file).
"""
import json
import os
import sqlite3

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-structural-pk-secret-long-enough-value-1")
os.environ.setdefault("USER_ID_SALT", "test-structural-pk-salt-long-enough-value-12")

import data.models as models
from data.relationship_service import (
    STRUCTURAL_INFERENCE_METHOD,
    discover_structural_pk_candidates,
)

_NOW = "2026-07-15T00:00:00+00:00"


def _db_conn(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _col(name, data_type, raw_type, *, is_pk=False, is_identity=False):
    return {
        "column_name": name, "ordinal_position": 1, "data_type": data_type,
        "raw_type": raw_type, "is_nullable": not is_pk, "is_primary_key": is_pk,
        "is_identity": is_identity, "max_length": None, "precision": None,
        "scale": None, "default_value": None,
    }


def _table(table_fqn, columns, pk_column_names=()):
    schema, name = table_fqn.split(".")
    return {
        "table_name": name, "schema_name": schema, "table_fqn": table_fqn,
        "table_type": "TABLE", "row_count_estimate": 10,
        "columns": columns,
        "primary_keys": [{"column_name": c, "key_ordinal": i + 1} for i, c in enumerate(pk_column_names)],
        "foreign_keys": [],
    }


# Mirrors the real CCPP scenario this feature was built to fix:
# ADF_Enrollment_Tracking has ClassID/PathID (plain INTEGER columns, not PK)
# that exactly name-match ADF_Class.ClassID / ADF_Path.PathID (both declared
# PKs) — plus one incompatible-type decoy and one non-PK-target decoy.
_SNAPSHOT_TABLES = [
    _table("dbo.ADF_Enrollment_Tracking", [
        _col("ET_ID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("ClassID", "INTEGER", "int"),
        _col("PathID", "INTEGER", "int"),
        # Decoy: name-matches ADF_Path.PathLabel (not a PK) — must be rejected.
        _col("PathLabel", "TEXT", "nvarchar"),
    ], pk_column_names=["ET_ID"]),
    _table("dbo.ADF_Class", [
        _col("ClassID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("ClassName", "TEXT", "nvarchar"),
    ], pk_column_names=["ClassID"]),
    _table("dbo.ADF_Path", [
        _col("PathID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("PathLabel", "TEXT", "nvarchar"),
    ], pk_column_names=["PathID"]),
    # Decoy: same column name as a PK ("ClassID") but an incompatible type —
    # must be rejected on type-compatibility, not on name.
    _table("dbo.Decoy_TypeMismatch", [
        _col("ID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("ClassID", "TEXT", "nvarchar"),
    ], pk_column_names=["ID"]),
    # Decoy pair for the minimum-specificity gate: a bare "ID" primary key
    # matched by another table's own bare "ID" column — must be rejected
    # even though it otherwise satisfies every other criterion.
    _table("dbo.Decoy_GenericPK", [
        _col("ID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("Notes", "TEXT", "nvarchar"),
    ], pk_column_names=["ID"]),
    _table("dbo.Decoy_GenericSource", [
        _col("RowID", "INTEGER", "int", is_pk=True, is_identity=True),
        _col("ID", "INTEGER", "int"),
    ], pk_column_names=["RowID"]),
]


def env(tmp_path, monkeypatch, *, tables=None):
    db_path = str(tmp_path / "structural_pk.db")
    import data.db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    models.init_db()
    monkeypatch.setattr("data.relationship_service.get_connection", lambda p=db_path: _db_conn(p))

    conn = _db_conn(db_path)
    conn.execute(
        "INSERT INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, "
        " encrypted_config_json, config_schema_version, capabilities_json, "
        " metadata_json, source_status, is_active, created_at, updated_at) "
        "VALUES (1,'u1','Test','mssql','RELATIONAL','{}',1,'[]','{}','ACTIVE',1,?,?)",
        (_NOW, _NOW),
    )
    snapshot_json = json.dumps({
        "source_id": 1, "source_type": "mssql", "discovered_at": _NOW,
        "schemas": [{"schema_name": "dbo", "tables": tables if tables is not None else _SNAPSHOT_TABLES}],
    })
    conn.execute(
        "INSERT INTO schema_snapshots "
        "(id, source_id, snapshot_version, source_type, table_count, snapshot_json, "
        " discovered_at, created_at) VALUES (1,1,1,'mssql',?,?,?,?)",
        (len(tables if tables is not None else _SNAPSHOT_TABLES), snapshot_json, _NOW, _NOW),
    )
    conn.commit()
    conn.close()
    return db_path


def _relationship_rows(db_path):
    conn = _db_conn(db_path)
    rows = conn.execute(
        "SELECT * FROM table_relationships WHERE source_id=1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _find(rows, from_col, to_table_fqn, to_col):
    return next(
        (r for r in rows if r["from_column"] == from_col
         and r["to_table_fqn"] == to_table_fqn and r["to_column"] == to_col),
        None,
    )


# ---------------------------------------------------------------------------
# 1 — ADF_Enrollment_Tracking.PathID -> ADF_Path.PathID
# ---------------------------------------------------------------------------

def test_pathid_creates_pending_structural_candidate(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    result = discover_structural_pk_candidates(1, "u1")
    assert result is not None
    assert result["candidates_persisted"] >= 1

    rows = _relationship_rows(db)
    row = _find(rows, "PathID", "dbo.ADF_Path", "PathID")
    assert row is not None
    assert row["from_table_fqn"] == "dbo.ADF_Enrollment_Tracking"
    assert row["relationship_status"] == "PENDING"
    assert row["inference_method"] == STRUCTURAL_INFERENCE_METHOD
    evidence = json.loads(row["evidence_json"])
    signals = {e["signal"] for e in evidence["evidence"]}
    assert signals == {"exact_name_match", "datatype_compatible", "target_primary_key"}


# ---------------------------------------------------------------------------
# 2 — ADF_Enrollment_Tracking.ClassID -> ADF_Class.ClassID
# ---------------------------------------------------------------------------

def test_classid_creates_pending_structural_candidate(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    discover_structural_pk_candidates(1, "u1")

    rows = _relationship_rows(db)
    row = _find(rows, "ClassID", "dbo.ADF_Class", "ClassID")
    assert row is not None
    assert row["from_table_fqn"] == "dbo.ADF_Enrollment_Tracking"
    assert row["relationship_status"] == "PENDING"
    assert row["inference_method"] == STRUCTURAL_INFERENCE_METHOD


# ---------------------------------------------------------------------------
# 3 — incompatible data types rejected
# ---------------------------------------------------------------------------

def test_incompatible_data_types_rejected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    result = discover_structural_pk_candidates(1, "u1")
    assert result["candidates_rejected_type"] >= 1

    rows = _relationship_rows(db)
    # Decoy_TypeMismatch.ClassID (TEXT) must NOT produce a candidate against
    # ADF_Class.ClassID (INTEGER) despite the exact name match.
    bad = _find(rows, "ClassID", "dbo.ADF_Class", "ClassID")
    assert bad is None or bad["from_table_fqn"] != "dbo.Decoy_TypeMismatch"


# ---------------------------------------------------------------------------
# 4 — non-PK target rejected
# ---------------------------------------------------------------------------

def test_non_pk_target_rejected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    discover_structural_pk_candidates(1, "u1")

    rows = _relationship_rows(db)
    # ADF_Enrollment_Tracking.PathLabel exactly name-matches ADF_Path.PathLabel,
    # which is NOT a declared primary key — must never produce a candidate.
    bad = _find(rows, "PathLabel", "dbo.ADF_Path", "PathLabel")
    assert bad is None


# ---------------------------------------------------------------------------
# 5 — bare generic PK name rejected (minimum-specificity gate)
# ---------------------------------------------------------------------------

def test_bare_generic_pk_name_rejected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    result = discover_structural_pk_candidates(1, "u1")
    assert result["candidates_rejected_generic_name"] >= 1

    rows = _relationship_rows(db)
    # Decoy_GenericSource.ID exactly name-matches Decoy_GenericPK.ID, which
    # IS a declared primary key — but "ID" alone is a bare generic word with
    # no qualifying prefix, so no candidate may be created.
    bad = _find(rows, "ID", "dbo.Decoy_GenericPK", "ID")
    assert bad is None

    # A prefixed name on the very same real scenario (PathID) must still be
    # created — the gate only excludes bare words, not every "*ID" column.
    good = _find(rows, "PathID", "dbo.ADF_Path", "PathID")
    assert good is not None


# ---------------------------------------------------------------------------
# 6 — duplicate candidates not created
# ---------------------------------------------------------------------------

def test_duplicate_candidates_not_created(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    first = discover_structural_pk_candidates(1, "u1")
    assert first["candidates_persisted"] >= 1

    second = discover_structural_pk_candidates(1, "u1")
    assert second["candidates_persisted"] == 0
    assert second["candidates_skipped_existing"] >= 1

    rows = _relationship_rows(db)
    matches = [
        r for r in rows
        if r["from_table_fqn"] == "dbo.ADF_Enrollment_Tracking"
        and r["from_column"] == "PathID" and r["to_table_fqn"] == "dbo.ADF_Path"
    ]
    assert len(matches) == 1
