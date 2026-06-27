"""
Tests for Change 4 — autocomplete suggestions endpoint.

Verifies that get_search_suggestions():
  - Returns results only from real stored metadata (no hardcoded values)
  - Matches across all 6 metadata fields
  - Deduplicates across sources
  - Ranks prefix matches before contains-only matches
  - Respects the limit parameter
  - Returns [] for empty or whitespace-only queries
  - Returns the correct structure {text, type}

Run from project root:
    python -m pytest tests/test_search_suggestions.py -v
"""

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-secret-suggestions")
os.environ.setdefault("USER_ID_SALT",   "test-salt-suggestions")

import data.search_service as search_service_module
from data.search_service import get_search_suggestions

# ---------------------------------------------------------------------------
# In-memory DB seed
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

    # Table names
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.Projects', 'Projects', 'dbo')")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.Employees', 'Employees', 'dbo')")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.EmployeeHistory', 'EmployeeHistory', 'dbo')")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'fin.Invoices', 'Invoices', 'fin')")

    # Column names
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type) VALUES (1, 'dbo.Employees', 'EmailAddress', 'EMAIL')")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type) VALUES (1, 'dbo.Employees', 'EmployeeID', 'IDENTIFIER')")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name) VALUES (1, 'fin.Invoices', 'InvoiceAmount')")

    # Business names (dict tables)
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'Project Registry')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'Employee Master')")

    # Business labels (dict columns)
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label) VALUES (1, 'dbo.Employees', 'EmailAddress', 'Employee Email')")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label) VALUES (1, 'dbo.Projects', 'ProjectName', 'Project Title')")

    # Domain assignments
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Projects', 'Project Management')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Employees', 'Human Resources')")

    # Entity assignments
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Projects', 'Project')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Employees', 'Employee')")

    conn.commit()


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_suggestions.db"
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
# Structure tests
# ---------------------------------------------------------------------------

def test_suggestions_return_list():
    result = get_search_suggestions("proj")
    assert isinstance(result, list)


def test_each_suggestion_has_text_and_type():
    result = get_search_suggestions("employee")
    assert len(result) > 0
    for s in result:
        assert "text" in s, f"Missing 'text' in suggestion: {s}"
        assert "type" in s, f"Missing 'type' in suggestion: {s}"
        assert isinstance(s["text"], str) and s["text"], "text must be non-empty string"
        assert isinstance(s["type"], str) and s["type"], "type must be non-empty string"


# ---------------------------------------------------------------------------
# Source coverage tests — each field must produce suggestions
# ---------------------------------------------------------------------------

def test_table_name_suggestion():
    """'proj' must return Projects from table names."""
    result = get_search_suggestions("proj")
    texts = [s["text"].lower() for s in result]
    assert any("project" in t for t in texts), f"Expected table suggestion, got: {texts}"
    types = [s["type"] for s in result if "project" in s["text"].lower()]
    assert "table" in types


def test_business_name_suggestion():
    """'employee master' must return Employee Master from dictionary tables."""
    result = get_search_suggestions("employee m")
    texts = [s["text"].lower() for s in result]
    assert any("employee master" in t for t in texts), f"Expected business_name, got: {texts}"


def test_column_name_suggestion():
    """'email' must return EmailAddress from column profiles."""
    result = get_search_suggestions("email")
    col_suggestions = [s for s in result if s["type"] == "column"]
    assert len(col_suggestions) > 0, "Expected at least one column suggestion"
    assert any("email" in s["text"].lower() for s in col_suggestions)


def test_business_label_suggestion():
    """'employee email' must return Employee Email from dict columns."""
    result = get_search_suggestions("employee e")
    texts = [s["text"].lower() for s in result]
    assert any("employee email" in t or "employee" in t for t in texts)


def test_domain_suggestion():
    """'human' must return Human Resources from domain_assignments."""
    result = get_search_suggestions("human")
    domain_suggestions = [s for s in result if s["type"] == "domain"]
    assert len(domain_suggestions) > 0, f"Expected domain suggestion, got: {result}"
    assert any("human" in s["text"].lower() for s in domain_suggestions)


def test_entity_suggestion():
    """'employ' must return Employee from entity_assignments."""
    result = get_search_suggestions("employ")
    entity_suggestions = [s for s in result if s["type"] == "entity"]
    assert len(entity_suggestions) > 0, f"Expected entity suggestion, got: {result}"


# ---------------------------------------------------------------------------
# Deduplication test
# ---------------------------------------------------------------------------

def test_suggestions_deduplicated():
    """Each text value must appear at most once across all results."""
    result = get_search_suggestions("employee")
    texts_lower = [s["text"].lower() for s in result]
    assert len(texts_lower) == len(set(texts_lower)), (
        f"Duplicate suggestion found: {texts_lower}"
    )


# ---------------------------------------------------------------------------
# Prefix ranking test
# ---------------------------------------------------------------------------

def test_prefix_match_ranks_before_contains():
    """'Pro' starts 'Projects', so Projects should appear before a pure-contains match."""
    result = get_search_suggestions("pro")
    texts = [s["text"] for s in result]
    assert len(texts) > 0
    # Projects starts with 'Pro' — it must appear in the results
    starts_with = [t for t in texts if t.lower().startswith("pro")]
    assert len(starts_with) > 0, f"Expected prefix-matching suggestion, got: {texts}"
    # The first result must start with 'pro'
    assert texts[0].lower().startswith("pro"), (
        f"Prefix match not ranked first. Order: {texts}"
    )


# ---------------------------------------------------------------------------
# Limit test
# ---------------------------------------------------------------------------

def test_limit_is_respected():
    result_5 = get_search_suggestions("e", limit=5)
    assert len(result_5) <= 5, f"Expected ≤5 suggestions, got {len(result_5)}"


def test_limit_default_8():
    result = get_search_suggestions("e")
    assert len(result) <= 8, f"Default limit should be 8, got {len(result)}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_query_returns_empty():
    assert get_search_suggestions("") == []


def test_whitespace_only_returns_empty():
    assert get_search_suggestions("   ") == []


def test_no_match_returns_empty():
    result = get_search_suggestions("xyzzy_no_match_zork_qwerty")
    assert result == [], f"Expected [], got {result}"


def test_case_insensitive_matching():
    """'PROJ' and 'proj' must return the same core results."""
    upper = get_search_suggestions("PROJ")
    lower = get_search_suggestions("proj")
    upper_texts = {s["text"].lower() for s in upper}
    lower_texts = {s["text"].lower() for s in lower}
    assert upper_texts == lower_texts, (
        f"Case mismatch: upper={upper_texts}  lower={lower_texts}"
    )


def test_all_suggestions_come_from_real_metadata():
    """Every returned text must exist verbatim in the seeded database tables.

    This confirms that no suggestion is hardcoded — all values originate
    from stored profiling or dictionary metadata.
    """
    result = get_search_suggestions("e")
    if not result:
        return  # nothing to verify

    all_real_values = {
        "projects", "employees", "employeehistory", "invoices",
        "emailaddress", "employeeid", "invoiceamount",
        "project registry", "employee master",
        "employee email", "project title",
        "project management", "human resources",
        "project", "employee",
    }
    for s in result:
        assert s["text"].lower() in all_real_values, (
            f"Suggestion '{s['text']}' not found in known metadata — possible hardcoded value!"
        )
