"""
Focused tests — Phase 7, Step 6E3B1, Part 3: data-dictionary / view-profile
reconciliation (Option B — sql_view_profiles is the authoritative view-
profile store; data/dictionary_service.py reads through to it for VIEW
objects it has no human-curated row for).

Reuses the real init_db() schema + the same get_connection monkeypatch
idiom as tests/test_data_view_profile_service.py.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3

import pytest

import data.models as models_module
import data.dictionary_service as dictionary_service_module
import data.view_profile_service as view_profile_service_module
import core.sqlservice.retrieval_store as retrieval_store_module
import core.sqlservice.schema_object_authority as authority_module
import data.db as data_db_module
from data.dictionary_service import get_table_dictionary, list_dictionary_tables
from data.view_profile_service import run_deterministic_view_profiling
from core.sqlservice.schema_object_authority import clear_schema_object_authority_cache

SOURCE_ID = 90001
USER_ID = "alice"
NOW = "2026-10-01T00:00:00Z"


def _make_connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def template_db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("dict_recon_template") / "template.db")
    original = models_module.get_connection
    models_module.get_connection = lambda: _make_connection(path)
    try:
        models_module.init_db()
    finally:
        models_module.get_connection = original
    return path


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    path = str(tmp_path / "fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(dictionary_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(view_profile_service_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(retrieval_store_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(authority_module, "get_connection", lambda: _make_connection(path))
    monkeypatch.setattr(data_db_module, "get_connection", lambda: _make_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_authority_cache():
    # Same test-pollution risk and fix as tests/test_data_view_profile_
    # service.py's own fixture of this name -- see its docstring.
    clear_schema_object_authority_cache()
    yield
    clear_schema_object_authority_cache()


def _seed(db_path, *, build_id=1, schema_fingerprint="fp-1", view_definition="SELECT u.Id FROM dbo.Users u"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO data_source_connections "
        "(id, user_id, display_name, source_type, source_category, encrypted_config_json, created_at, updated_at) "
        "VALUES (?, ?, 'Test Source', 'mssql', 'database', '{}', ?, ?)",
        (SOURCE_ID, USER_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_snapshots (id, source_id, snapshot_version, source_type, snapshot_json, discovered_at, created_at) "
        "VALUES (1, ?, 1, 'mssql', '{}', ?, ?)",
        (SOURCE_ID, NOW, NOW),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sql_retrieval_builds "
        "(id, source_id, snapshot_id, snapshot_version, fingerprint_version, schema_fingerprint, "
        " table_set_hash, document_set_hash, status, document_count, created_at, completed_at) "
        "VALUES (?, ?, 1, 1, 1, ?, 'ts-hash', 'ds-hash', 'READY', 2, ?, ?)",
        (build_id, SOURCE_ID, schema_fingerprint, NOW, NOW),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, document_text, metadata_json, created_at) "
        "VALUES (?, ?, 'dbo.Users', 'TABLE', 1, ?, 'text', ?, ?)",
        (build_id, SOURCE_ID, hashlib.sha256(b"users").hexdigest(),
         json.dumps({"column_names": ["Id"], "primary_key_columns": ["Id"]}), NOW),
    )
    conn.execute(
        "INSERT OR REPLACE INTO sql_retrieval_documents "
        "(build_id, source_id, object_fqn, object_type, retrieval_tier, document_hash, document_text, metadata_json, created_at) "
        "VALUES (?, ?, 'dbo.v_test', 'VIEW', 1, ?, 'text', '{}', ?)",
        (build_id, SOURCE_ID, hashlib.sha256(b"vtest").hexdigest(), NOW),
    )
    definition_sql = f"CREATE VIEW dbo.v_test AS {view_definition}"
    checksum = hashlib.sha256(definition_sql.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, "
        " referenced_tables_json, restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, 'dbo.v_test', ?, ?, ?, NULL, 0.0, 1, ?) "
        "ON CONFLICT(source_id, table_fqn) DO UPDATE SET "
        "definition_sql=excluded.definition_sql, definition_checksum=excluded.definition_checksum, "
        "discovered_at=excluded.discovered_at",
        (SOURCE_ID, definition_sql, checksum, json.dumps(["dbo.Users"]), NOW),
    )
    conn.commit()
    conn.close()


def test_get_table_dictionary_returns_none_for_view_with_no_profile_yet(db_path):
    _seed(db_path)
    assert get_table_dictionary(SOURCE_ID, USER_ID, "dbo.v_test") is None


def test_get_table_dictionary_reads_through_to_view_profile(db_path):
    _seed(db_path)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    result = get_table_dictionary(SOURCE_ID, USER_ID, "dbo.v_test")

    assert result is not None
    assert result["table"]["table_type"] == "VIEW"
    assert result["table"]["source"] == "sql_view_profiles"
    assert result["table"]["completion_status"] == "COMPLETE"
    assert any(c["column_name"] == "Id" for c in result["columns"])


def test_list_dictionary_tables_includes_views_from_profile_store(db_path):
    _seed(db_path)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    rows = list_dictionary_tables(SOURCE_ID, USER_ID)

    view_rows = [r for r in rows if r.get("table_fqn") == "dbo.v_test"]
    assert len(view_rows) == 1
    assert view_rows[0]["table_type"] == "VIEW"


def test_human_curated_dictionary_row_takes_precedence_over_synthesized_one(db_path):
    _seed(db_path)
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO data_dictionary_tables "
        "(source_id, snapshot_id, table_fqn, table_name, schema_name, table_type, business_name, "
        " is_approved, generation_method, created_at, updated_at) "
        "VALUES (?, 1, 'dbo.v_test', 'v_test', 'dbo', 'VIEW', 'Human Curated Name', 1, 'manual', ?, ?)",
        (SOURCE_ID, NOW, NOW),
    )
    conn.commit()
    conn.close()

    result = get_table_dictionary(SOURCE_ID, USER_ID, "dbo.v_test")
    rows = list_dictionary_tables(SOURCE_ID, USER_ID)

    assert result["table"]["business_name"] == "Human Curated Name"
    matching = [r for r in rows if r["table_fqn"] == "dbo.v_test"]
    assert len(matching) == 1  # never duplicated
    assert matching[0]["business_name"] == "Human Curated Name"


def test_active_build_invalidation_stale_profile_not_served_as_current(db_path):
    # Profile a view under build/fingerprint 1...
    _seed(db_path, build_id=1, schema_fingerprint="fp-old")
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    first = get_table_dictionary(SOURCE_ID, USER_ID, "dbo.v_test")
    assert first is not None and first["table"]["completion_status"] == "COMPLETE"

    # ...the schema changes (new build/fingerprint), definition changes too.
    _seed(db_path, build_id=2, schema_fingerprint="fp-new", view_definition="SELECT u.Id, u.Name FROM dbo.Users u")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sql_retrieval_documents SET metadata_json = ? WHERE object_fqn = 'dbo.Users'",
        (json.dumps({"column_names": ["Id", "Name"], "primary_key_columns": ["Id"]}),),
    )
    conn.commit()
    conn.close()

    # Re-profiling under the NEW build produces a fresh row for the new
    # build_id/schema_fingerprint -- the dictionary read-through always
    # reflects whatever is currently persisted, never a stale snapshot.
    run_deterministic_view_profiling(SOURCE_ID, USER_ID)
    second = get_table_dictionary(SOURCE_ID, USER_ID, "dbo.v_test")

    assert second is not None
    assert any(c["column_name"] == "Name" for c in second["columns"])
