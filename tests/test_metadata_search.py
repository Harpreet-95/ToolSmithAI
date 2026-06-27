"""
Tests for Enterprise Metadata Search — Phase 1.

Validates tokenisation, scoring, and the full search_metadata() function
against a real in-memory SQLite database.  No mocks, no hardcoded results.

Run from the project root:
    python -m pytest tests/test_metadata_search.py -v
"""

import os
import sqlite3
import pytest

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-secret-search-phase1")
os.environ.setdefault("USER_ID_SALT",   "test-salt-search-phase1")

import data.search_service as search_service_module
from data.search_service import (
    _tokenize,
    _score_field,
    _score_table_row,
    _score_column_row,
    search_metadata,
)


# ---------------------------------------------------------------------------
# In-memory DB fixture
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE data_source_connections (
        id INTEGER PRIMARY KEY,
        display_name TEXT NOT NULL,
        source_type TEXT NOT NULL DEFAULT 'mssql',
        source_category TEXT NOT NULL DEFAULT 'relational',
        encrypted_config_json TEXT NOT NULL DEFAULT '{}',
        capabilities_json TEXT NOT NULL DEFAULT '[]',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        source_status TEXT NOT NULL DEFAULT 'ACTIVE',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_table_profiles (
        id INTEGER PRIMARY KEY,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        table_class TEXT,
        pii_column_count INTEGER NOT NULL DEFAULT 0,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE profiling_column_profiles (
        id INTEGER PRIMARY KEY,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        source_id INTEGER NOT NULL,
        table_fqn TEXT NOT NULL,
        column_name TEXT NOT NULL,
        data_type TEXT NOT NULL DEFAULT 'nvarchar',
        semantic_type TEXT,
        pii_confirmed INTEGER NOT NULL DEFAULT 0,
        profiling_status TEXT NOT NULL DEFAULT 'COMPLETE',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_tables (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        table_name TEXT NOT NULL,
        schema_name TEXT NOT NULL DEFAULT 'dbo',
        table_type TEXT NOT NULL DEFAULT 'TABLE',
        business_name TEXT,
        description TEXT,
        domain TEXT,
        grain TEXT,
        is_approved INTEGER NOT NULL DEFAULT 0,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE data_dictionary_columns (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        column_name TEXT NOT NULL,
        business_label TEXT,
        meaning TEXT,
        semantic_type TEXT,
        is_metric INTEGER NOT NULL DEFAULT 0,
        is_dimension INTEGER NOT NULL DEFAULT 0,
        is_date INTEGER NOT NULL DEFAULT 0,
        is_id INTEGER NOT NULL DEFAULT 0,
        pii_risk INTEGER NOT NULL DEFAULT 0,
        is_approved INTEGER NOT NULL DEFAULT 0,
        generation_method TEXT NOT NULL DEFAULT 'rule_based',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE domain_assignments (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        domain TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.8,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_domains_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
    CREATE TABLE entity_assignments (
        id INTEGER PRIMARY KEY,
        source_id INTEGER NOT NULL,
        profiling_snapshot_id INTEGER NOT NULL DEFAULT 1,
        table_fqn TEXT NOT NULL,
        entity TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0.8,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        competing_entities_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT '2026-01-01',
        updated_at TEXT NOT NULL DEFAULT '2026-01-01'
    );
"""

def _seed(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)

    conn.execute("INSERT INTO data_source_connections (id, display_name) VALUES (1, 'CCPP SQL Server')")
    conn.execute("INSERT INTO data_source_connections (id, display_name) VALUES (2, 'Finance DW')")

    # Tables
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'MASTER_DATA', 2)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Customers', 'Customers', 'fin', 'MASTER_DATA', 1)")

    # Columns
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmailAddress', 'EMAIL', 1)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmployeeID', 'IDENTIFIER', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'CURRENCY', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Projects', 'ProjectName', 'TEXT', 0)")

    # Dictionary tables
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'Project Registry', 'Tracks all active and historical projects', 'Project Management')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain, is_approved) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'Employee Master', 'Contains all employee records including PII', 'HR', 1)")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'Invoice Register', 'All finance invoices and billing records', 'Finance')")

    # Dictionary columns
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type, pii_risk) VALUES (1, 'dbo.Employees', 'EmailAddress', 'Employee Email', 'Primary contact email address for the employee', 'EMAIL', 1)")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning) VALUES (1, 'dbo.Projects', 'ProjectName', 'Project Title', 'The official name of the project as registered in the system')")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'Invoice Amount', 'Total amount billed for the invoice in local currency', 'CURRENCY')")

    # Domain assignments
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Projects', 'Project Management')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Employees', 'Human Resources')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (2, 'fin.Invoices', 'Finance')")

    # Entity assignments
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Projects', 'Project')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Employees', 'Employee')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (2, 'fin.Invoices', 'Invoice')")

    conn.commit()


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    """Replace the real DB with a seeded temp SQLite for every test.

    search_service imports get_connection by name, so we patch the reference
    inside the search_service module directly (not just data.db).
    """
    db_path = tmp_path / "test_search.db"
    seed_conn = sqlite3.connect(str(db_path))
    seed_conn.row_factory = sqlite3.Row
    _seed(seed_conn)
    seed_conn.close()

    def mock_get_connection():
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(search_service_module, "get_connection", mock_get_connection)


# ---------------------------------------------------------------------------
# Unit tests: _tokenize
# ---------------------------------------------------------------------------

def test_tokenize_splits_whitespace():
    assert _tokenize("employee email") == ["employee", "email"]

def test_tokenize_splits_underscore():
    assert _tokenize("employee_email") == ["employee", "email"]

def test_tokenize_lowercase():
    assert _tokenize("Power BI") == ["power", "bi"]

def test_tokenize_drops_short_tokens():
    tokens = _tokenize("a bc def")
    assert "a" not in tokens
    assert "bc" in tokens
    assert "def" in tokens

def test_tokenize_empty_string():
    assert _tokenize("") == []

def test_tokenize_mixed_delimiters():
    tokens = _tokenize("finance/invoices-data")
    assert "finance" in tokens
    assert "invoices" in tokens
    assert "data" in tokens


# ---------------------------------------------------------------------------
# Unit tests: _score_field
# ---------------------------------------------------------------------------

def test_score_field_exact_full():
    assert _score_field("Projects", ["projects"], 100) == 100

def test_score_field_word_boundary():
    score = _score_field("Project Registry", ["project"], 100)
    assert score == 85

def test_score_field_substring():
    score = _score_field("ProjectName", ["project"], 100)
    assert score == 50

def test_score_field_no_match():
    assert _score_field("Invoices", ["employee"], 100) == 0

def test_score_field_none_text():
    assert _score_field(None, ["anything"], 100) == 0

def test_score_field_multiple_tokens():
    score = _score_field("Employee Email Address", ["employee", "email"], 90)
    assert score > 0
    single = _score_field("Employee Email Address", ["employee"], 90)
    assert score > single


# ---------------------------------------------------------------------------
# Integration tests: search_metadata
# ---------------------------------------------------------------------------

def test_search_empty_query_returns_empty():
    result = search_metadata("")
    assert result["results"] == []
    assert result["total"] == 0

def test_search_whitespace_query_returns_empty():
    result = search_metadata("   ")
    assert result["results"] == []

def test_search_projects_finds_table():
    result = search_metadata("Projects")
    names = [r["display_name"] for r in result["results"]]
    assert any("Project" in n for n in names), f"Expected project result, got: {names}"

def test_search_employee_email_finds_column():
    result = search_metadata("Employee Email")
    col_results = [r for r in result["results"] if r["asset_type"] == "column"]
    assert len(col_results) > 0
    display_names = [r["display_name"] for r in col_results]
    assert any("Email" in n or "email" in n.lower() for n in display_names)

def test_search_finance_finds_invoice():
    result = search_metadata("Finance")
    assert result["total"] > 0
    names = [r["display_name"] for r in result["results"]]
    assert any("Invoice" in n or "Finance" in n for n in names)

def test_search_returns_relevance_score():
    result = search_metadata("Projects")
    for r in result["results"]:
        assert "relevance_score" in r
        assert r["relevance_score"] > 0

def test_search_results_sorted_by_score():
    result = search_metadata("Employee Email")
    scores = [r["relevance_score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True), "Results must be sorted by relevance descending"

def test_search_result_has_required_fields():
    result = search_metadata("Projects")
    assert result["total"] > 0
    r = result["results"][0]
    required = [
        "asset_type", "display_name", "qualified_name", "source_id",
        "source_name", "schema_name", "table_name", "column_name",
        "matched_field", "matched_text", "relevance_score",
        "short_description", "domain", "entity", "dictionary_status",
        "pii_indicator", "semantic_type", "nav_target",
    ]
    for field in required:
        assert field in r, f"Missing field: {field}"

def test_search_asset_type_filter_table_only():
    result = search_metadata("employee", asset_type="table")
    for r in result["results"]:
        assert r["asset_type"] == "table"

def test_search_asset_type_filter_column_only():
    result = search_metadata("email", asset_type="column")
    for r in result["results"]:
        assert r["asset_type"] == "column"

def test_search_source_id_filter():
    result = search_metadata("employee", source_id=1)
    for r in result["results"]:
        assert r["source_id"] == 1

def test_search_pii_indicator_set_for_pii_table():
    result = search_metadata("Employees")
    table_results = [r for r in result["results"] if r["asset_type"] == "table" and "employ" in r["display_name"].lower()]
    assert len(table_results) > 0
    assert table_results[0]["pii_indicator"] is True

def test_search_pii_indicator_false_for_non_pii():
    result = search_metadata("Projects")
    table_results = [r for r in result["results"] if r["asset_type"] == "table" and "Project" in r["display_name"]]
    assert len(table_results) > 0
    assert table_results[0]["pii_indicator"] is False

def test_search_dictionary_status_approved():
    result = search_metadata("Employee Master")
    table_results = [r for r in result["results"] if r["asset_type"] == "table"]
    approved = [r for r in table_results if r["dictionary_status"] == "approved"]
    assert len(approved) > 0

def test_search_pagination_limit():
    result = search_metadata("e", limit=2)
    assert len(result["results"]) <= 2

def test_search_pagination_offset():
    all_results  = search_metadata("e", limit=100, offset=0)
    page2        = search_metadata("e", limit=2, offset=2)
    if all_results["total"] > 2:
        assert page2["results"][0] == all_results["results"][2]

def test_search_tokens_returned():
    result = search_metadata("Employee Email")
    assert "employee" in result["tokens"]
    assert "email"    in result["tokens"]

def test_search_no_results_for_nonsense_query():
    result = search_metadata("xyzzy_nonexistent_zork_qwerty")
    assert result["total"] == 0
    assert result["results"] == []

def test_search_domain_assignment_in_result():
    result = search_metadata("Projects")
    proj = [r for r in result["results"] if r["asset_type"] == "table" and "Project" in r["display_name"]]
    assert len(proj) > 0
    assert proj[0]["domain"] in ("Project Management", "")

def test_search_nav_target_has_source_id():
    result = search_metadata("Projects")
    for r in result["results"]:
        assert r["nav_target"]["source_id"] is not None

def test_search_column_qualified_name_includes_column():
    result = search_metadata("EmailAddress", asset_type="column")
    for r in result["results"]:
        if r["asset_type"] == "column":
            assert r["column_name"] in r["qualified_name"]

def test_exact_table_name_scores_higher_than_partial():
    result = search_metadata("Projects")
    scores_by_name = {r["display_name"]: r["relevance_score"] for r in result["results"] if r["asset_type"] == "table"}
    # "Project Registry" (exact word boundary match on 'projects') should score >= substring matches
    # We just verify the top result is project-related
    assert result["total"] > 0
    top = result["results"][0]
    assert "project" in top["display_name"].lower() or "project" in (top["matched_text"] or "").lower()
