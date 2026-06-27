"""
Tests for the synonym expansion system — Change 1.

Covers:
  - _SynonymExpander unit behaviour
  - _expand_tokens unit behaviour
  - Integration: synonym terms find real seeded metadata
  - Backward compatibility: existing direct queries still work

Run from the project root:
    python -m pytest tests/test_synonyms.py -v
"""

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-secret-synonyms")
os.environ.setdefault("USER_ID_SALT",   "test-salt-synonyms")

import data.search_service as search_service_module
from data.search_service import (
    _SynonymExpander,
    _SYNONYM_EXPANDER,
    _expand_tokens,
    search_metadata,
)


# ---------------------------------------------------------------------------
# Minimal seeded DB — mirrors the fixture in test_metadata_search.py
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

    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'MASTER_DATA', 2)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Customers', 'Customers', 'fin', 'MASTER_DATA', 1)")

    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmailAddress', 'EMAIL', 1)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmployeeID', 'IDENTIFIER', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'CURRENCY', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Projects', 'ProjectName', 'TEXT', 0)")

    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'Project Registry', 'Tracks all active and historical projects', 'Project Management')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain, is_approved) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'Employee Master', 'Contains all employee records including PII', 'HR', 1)")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'Invoice Register', 'All finance invoices and billing records', 'Finance')")

    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type, pii_risk) VALUES (1, 'dbo.Employees', 'EmailAddress', 'Employee Email', 'Primary contact email address for the employee', 'EMAIL', 1)")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning) VALUES (1, 'dbo.Projects', 'ProjectName', 'Project Title', 'The official name of the project as registered in the system')")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'Invoice Amount', 'Total amount billed for the invoice in local currency', 'CURRENCY')")

    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Projects', 'Project Management')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Employees', 'Human Resources')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (2, 'fin.Invoices', 'Finance')")

    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Projects', 'Project')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Employees', 'Employee')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (2, 'fin.Invoices', 'Invoice')")

    conn.commit()


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_synonyms.db"
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
# Unit tests: _SynonymExpander
# ---------------------------------------------------------------------------

def test_expander_loads_from_file():
    """The module-level expander must load at least the spec synonym groups."""
    assert len(_SYNONYM_EXPANDER) >= 7, (
        f"Expected at least 7 synonym groups in synonyms.json, got {len(_SYNONYM_EXPANDER)}"
    )


def test_expander_expand_known_term():
    exp = _SynonymExpander([["customer", "client", "account"]])
    result = exp.expand("client")
    assert "customer" in result
    assert "client" in result
    assert "account" in result


def test_expander_expand_unknown_term_returns_self():
    exp = _SynonymExpander([["customer", "client"]])
    result = exp.expand("zzz_nonexistent")
    assert result == frozenset({"zzz_nonexistent"})


def test_expander_case_insensitive():
    exp = _SynonymExpander([["customer", "client"]])
    assert "customer" in exp.expand("CLIENT")
    assert "customer" in exp.expand("Client")
    assert "customer" in exp.expand("customer")


def test_expander_drops_single_char_terms():
    """Terms shorter than 2 characters must be excluded from groups."""
    exp = _SynonymExpander([["a", "bi", "report"]])
    # "a" is dropped; group has "bi" and "report" — still 2 valid terms
    assert "report" in exp.expand("bi")
    # "a" alone produces no synonym expansion
    assert exp.expand("a") == frozenset({"a"})


def test_expander_single_term_group_ignored():
    """A group with only one valid term provides no expansion."""
    exp = _SynonymExpander([["solo"]])
    result = exp.expand("solo")
    assert result == frozenset({"solo"})
    assert len(exp) == 0  # ignored — no expansion value


def test_expander_len_counts_groups():
    exp = _SynonymExpander([
        ["customer", "client"],
        ["email", "mail"],
        ["project", "programme"],
    ])
    assert len(exp) == 3


# ---------------------------------------------------------------------------
# Unit tests: _expand_tokens
# ---------------------------------------------------------------------------

def test_expand_tokens_no_synonyms_unchanged():
    """Tokens with no synonym mappings pass through unchanged."""
    result = _expand_tokens(["zzz_nosynonym"])
    assert result == ["zzz_nosynonym"]


def test_expand_tokens_adds_synonyms():
    """A token with a synonym group gets expanded with its synonyms."""
    result = _expand_tokens(["client"])
    assert "client" in result
    assert "customer" in result
    assert "account" in result


def test_expand_tokens_original_token_first():
    """The original query token must appear before any synonym additions."""
    result = _expand_tokens(["client"])
    assert result[0] == "client", "Original token must be first"


def test_expand_tokens_no_duplicates_when_synonym_already_in_query():
    """If the query contains both a term and its synonym, no duplication."""
    result = _expand_tokens(["client", "customer"])
    assert result.count("client") == 1
    assert result.count("customer") == 1


def test_expand_tokens_multiple_tokens_each_expanded():
    """Every token in the query is independently expanded."""
    result = _expand_tokens(["client", "mail"])
    assert "customer" in result   # synonym of "client"
    assert "email" in result      # synonym of "mail"


# ---------------------------------------------------------------------------
# Integration tests: synonym-driven search against seeded DB
# ---------------------------------------------------------------------------

def test_synonym_client_finds_customers_table():
    """Searching 'client' must find the Customers table via the customer synonym."""
    result = search_metadata("client")
    table_names = [
        r["table_name"].lower()
        for r in result["results"]
        if r["asset_type"] == "table"
    ]
    assert any("customer" in n for n in table_names), (
        f"Expected Customers table via 'client' synonym, got tables: {table_names}"
    )


def test_synonym_worker_finds_employees_table():
    """Searching 'worker' must find the Employees table via the employee synonym."""
    result = search_metadata("worker")
    table_names = [
        r["table_name"].lower()
        for r in result["results"]
        if r["asset_type"] == "table"
    ]
    assert any("employee" in n for n in table_names), (
        f"Expected Employees table via 'worker' synonym, got tables: {table_names}"
    )


def test_synonym_mail_finds_email_column():
    """Searching 'mail' must find the EmailAddress column via the email synonym."""
    result = search_metadata("mail")
    col_names = [
        r["column_name"].lower()
        for r in result["results"]
        if r["asset_type"] == "column" and r["column_name"]
    ]
    assert any("email" in n for n in col_names), (
        f"Expected EmailAddress column via 'mail' synonym, got columns: {col_names}"
    )


def test_synonym_response_tokens_are_original_not_expanded():
    """The 'tokens' key in the response must contain only the original query tokens."""
    result = search_metadata("client")
    assert result["tokens"] == ["client"], (
        f"Expected original tokens ['client'], got {result['tokens']}"
    )


def test_synonym_backward_compat_projects_still_found():
    """Direct search for 'Projects' still finds the Projects table (no regression)."""
    result = search_metadata("Projects")
    names = [r["display_name"] for r in result["results"]]
    assert any("Project" in n for n in names), (
        f"Backward compat broken: 'Projects' returned no project results. Got: {names}"
    )
