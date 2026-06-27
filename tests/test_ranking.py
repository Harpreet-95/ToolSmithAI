"""
Tests for Change 2 — improved ranking (phrase bonus, multi-field bonus, match_reasons).

Covers:
  - match_reasons present and correct on every result
  - Exact phrase match triggers "Exact phrase match" reason and phrase bonus
  - Phrase-matching result outranks equivalent token-only result
  - Multi-field match outranks single-field match for same query
  - Synonym-expanded results still carry correct match_reasons
  - matched_field and matched_text remain present (backward compat)
  - Existing result sort order is still descending by relevance_score

Run from project root:
    python -m pytest tests/test_ranking.py -v
"""

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-secret-ranking")
os.environ.setdefault("USER_ID_SALT",   "test-salt-ranking")

import data.search_service as search_service_module
from data.search_service import (
    _SynonymExpander,
    _score_table_detailed,
    _score_column_detailed,
    _check_phrase,
    _multi_field_bonus,
    _PHRASE_BONUS,
    _MULTI_FIELD_BONUS_PER_FIELD,
    _TABLE_PHRASE_FIELDS,
    _COLUMN_PHRASE_FIELDS,
    search_metadata,
)


# ---------------------------------------------------------------------------
# Seeded DB — standard tables + two extra for controlled ranking tests
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

    # Standard tables
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'MASTER_DATA', 2)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'TRANSACTION', 0)")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name, table_class, pii_column_count) VALUES (2, 'fin.Customers', 'Customers', 'fin', 'MASTER_DATA', 1)")

    # Extra table for phrase ranking test — name contains exact phrase "Power BI"
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.PowerBIReports', 'Power BI Reports', 'dbo')")
    # Extra table for phrase ranking test — tokens "Power" and "BI" match separately but no phrase
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.PowerTools', 'Power Tools', 'dbo')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, description) VALUES (1, 'dbo.PowerTools', 'Power Tools', 'dbo', 'Advanced BI Analytics and reporting platform')")

    # Extra tables for multi-field bonus test — WidgetCatalog matches 3 fields, WidgetLog matches 1
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.WidgetCatalog', 'Widget Catalog', 'dbo')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description) VALUES (1, 'dbo.WidgetCatalog', 'Widget Catalog', 'dbo', 'Widget Management', 'All widgets tracked and maintained here')")
    conn.execute("INSERT INTO profiling_table_profiles (source_id, table_fqn, table_name, schema_name) VALUES (1, 'dbo.WidgetLog', 'Widget Log', 'dbo')")
    # WidgetLog has no dict entry — only table_name matches "widget"

    # Standard columns
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmailAddress', 'EMAIL', 1)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Employees', 'EmployeeID', 'IDENTIFIER', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'CURRENCY', 0)")
    conn.execute("INSERT INTO profiling_column_profiles (source_id, table_fqn, column_name, semantic_type, pii_confirmed) VALUES (1, 'dbo.Projects', 'ProjectName', 'TEXT', 0)")

    # Standard dictionary — tables
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'Project Registry', 'Tracks all active and historical projects', 'Project Management')")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain, is_approved) VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'Employee Master', 'Contains all employee records including PII', 'HR', 1)")
    conn.execute("INSERT INTO data_dictionary_tables (source_id, table_fqn, table_name, schema_name, business_name, description, domain) VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'Invoice Register', 'All finance invoices and billing records', 'Finance')")

    # Standard dictionary — columns
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type, pii_risk) VALUES (1, 'dbo.Employees', 'EmailAddress', 'Employee Email', 'Primary contact email address for the employee', 'EMAIL', 1)")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning) VALUES (1, 'dbo.Projects', 'ProjectName', 'Project Title', 'The official name of the project as registered in the system')")
    conn.execute("INSERT INTO data_dictionary_columns (source_id, table_fqn, column_name, business_label, meaning, semantic_type) VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'Invoice Amount', 'Total amount billed for the invoice in local currency', 'CURRENCY')")

    # Domain / entity assignments
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Projects', 'Project Management')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (1, 'dbo.Employees', 'Human Resources')")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain) VALUES (2, 'fin.Invoices', 'Finance')")

    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Projects', 'Project')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (1, 'dbo.Employees', 'Employee')")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity) VALUES (2, 'fin.Invoices', 'Invoice')")

    conn.commit()


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_ranking.db"
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
# Unit tests: new helpers
# ---------------------------------------------------------------------------

def test_score_table_detailed_returns_score_and_reasons():
    row = {"table_name": "Projects", "business_name": None, "description": None,
           "schema_name": None, "source_name": None, "table_class": None,
           "dict_domain": None, "assigned_domain": None, "assigned_entity": None}
    score, reasons = _score_table_detailed(row, ["projects"])
    assert score > 0
    assert "Table name" in reasons


def test_score_table_detailed_no_match_returns_empty_reasons():
    row = {"table_name": "Invoices", "business_name": None, "description": None,
           "schema_name": None, "source_name": None, "table_class": None,
           "dict_domain": None, "assigned_domain": None, "assigned_entity": None}
    score, reasons = _score_table_detailed(row, ["zzz_nomatch"])
    assert score == 0
    assert reasons == []


def test_score_column_detailed_returns_score_and_reasons():
    row = {"column_name": "EmailAddress", "business_label": "Employee Email",
           "meaning": "Contact email", "semantic_type": "EMAIL",
           "table_name": "Employees", "source_name": None,
           "assigned_domain": None, "assigned_entity": None}
    score, reasons = _score_column_detailed(row, ["email"])
    assert score > 0
    assert len(reasons) >= 1


def test_check_phrase_matches_substring():
    row = {"table_name": "Power BI Reports", "business_name": None, "description": None}
    assert _check_phrase(row, _TABLE_PHRASE_FIELDS, "Power BI") is True


def test_check_phrase_case_insensitive():
    row = {"table_name": "POWER BI REPORTS", "business_name": None, "description": None}
    assert _check_phrase(row, _TABLE_PHRASE_FIELDS, "power bi") is True


def test_check_phrase_no_match():
    row = {"table_name": "Power Tools", "business_name": None, "description": "BI Analytics"}
    assert _check_phrase(row, _TABLE_PHRASE_FIELDS, "Power BI") is False


def test_check_phrase_empty_query():
    row = {"table_name": "Projects", "business_name": None, "description": None}
    assert _check_phrase(row, _TABLE_PHRASE_FIELDS, "") is False


def test_multi_field_bonus_zero_for_one_field():
    assert _multi_field_bonus(1) == 0


def test_multi_field_bonus_scales_with_field_count():
    assert _multi_field_bonus(2) == _MULTI_FIELD_BONUS_PER_FIELD
    assert _multi_field_bonus(3) == 2 * _MULTI_FIELD_BONUS_PER_FIELD
    assert _multi_field_bonus(4) == 3 * _MULTI_FIELD_BONUS_PER_FIELD


def test_multi_field_bonus_zero_for_zero_fields():
    assert _multi_field_bonus(0) == 0


# ---------------------------------------------------------------------------
# Integration: match_reasons in search results
# ---------------------------------------------------------------------------

def test_match_reasons_present_on_every_result():
    result = search_metadata("Projects")
    assert result["total"] > 0
    for r in result["results"]:
        assert "match_reasons" in r, f"match_reasons missing from result: {r['display_name']}"
        assert isinstance(r["match_reasons"], list)
        assert len(r["match_reasons"]) > 0


def test_match_reasons_table_name_label():
    result = search_metadata("Projects")
    proj = [r for r in result["results"] if r["asset_type"] == "table" and "Project" in r["display_name"]]
    assert len(proj) > 0
    assert "Table name" in proj[0]["match_reasons"]


def test_match_reasons_business_name_label():
    result = search_metadata("Employee Master")
    emp = [r for r in result["results"] if r["asset_type"] == "table" and "Employee" in r["display_name"]]
    assert len(emp) > 0
    assert "Business name" in emp[0]["match_reasons"]


def test_match_reasons_column_name_label():
    result = search_metadata("EmailAddress", asset_type="column")
    assert result["total"] > 0
    col = result["results"][0]
    assert "Column name" in col["match_reasons"]


def test_match_reasons_entity_assignment_label():
    # Use singular "Project" so the entity assignment "Project" gets an exact match.
    # The tokenizer does not stem, so "Projects" (plural) would not match entity "Project".
    result = search_metadata("Project")
    proj = [r for r in result["results"] if r["asset_type"] == "table" and "Project" in r["display_name"]]
    assert len(proj) > 0
    assert "Entity assignment" in proj[0]["match_reasons"], (
        f"Expected 'Entity assignment' in reasons, got: {proj[0]['match_reasons']}"
    )


def test_match_reasons_domain_assignment_label():
    result = search_metadata("Human Resources")
    emp = [r for r in result["results"] if r["asset_type"] == "table"]
    assert len(emp) > 0
    assert "Domain assignment" in emp[0]["match_reasons"]


def test_match_reasons_dictionary_definition_label():
    result = search_metadata("invoice register")
    inv = [r for r in result["results"] if r["asset_type"] == "table" and "Invoice" in r["display_name"]]
    assert len(inv) > 0
    reasons = inv[0]["match_reasons"]
    assert "Business name" in reasons or "Dictionary definition" in reasons


def test_match_reasons_multiple_for_multi_field_result():
    """A result matching many fields must list multiple reasons."""
    result = search_metadata("Employee")
    emp = [r for r in result["results"] if r["asset_type"] == "table" and "Employ" in r["table_name"]]
    assert len(emp) > 0
    assert len(emp[0]["match_reasons"]) >= 2, (
        f"Expected multiple reasons for Employees table, got: {emp[0]['match_reasons']}"
    )


def test_match_reasons_column_multiple_fields():
    """EmailAddress column matches column_name, business_label, and meaning."""
    result = search_metadata("Employee Email", asset_type="column")
    col = [r for r in result["results"] if r["column_name"] == "EmailAddress"]
    assert len(col) > 0
    reasons = col[0]["match_reasons"]
    assert len(reasons) >= 2, f"Expected 2+ reasons, got: {reasons}"


# ---------------------------------------------------------------------------
# Integration: backward compatibility — matched_field / matched_text still present
# ---------------------------------------------------------------------------

def test_matched_field_still_present():
    result = search_metadata("Projects")
    for r in result["results"]:
        assert "matched_field" in r
        assert "matched_text" in r


def test_results_still_sorted_descending():
    result = search_metadata("Employee Email")
    scores = [r["relevance_score"] for r in result["results"]]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# Integration: phrase bonus
# ---------------------------------------------------------------------------

def test_phrase_bonus_reason_appears_when_phrase_matches():
    """Searching 'Employee Master' should produce 'Exact phrase match' reason for Employees."""
    result = search_metadata("Employee Master")
    emp = [r for r in result["results"] if r["asset_type"] == "table" and "Employee" in r["display_name"]]
    assert len(emp) > 0
    assert "Exact phrase match" in emp[0]["match_reasons"], (
        f"Expected 'Exact phrase match', got reasons: {emp[0]['match_reasons']}"
    )


def test_phrase_match_ranks_above_token_only_match():
    """Power BI Reports (phrase match) must outscore Power Tools (token-only match)."""
    result = search_metadata("Power BI")
    table_results = [r for r in result["results"] if r["asset_type"] == "table"]
    names = [r["table_name"] for r in table_results]
    assert "Power BI Reports" in names, f"Power BI Reports not found. Tables: {names}"
    assert "Power Tools" in names, f"Power Tools not found. Tables: {names}"

    score_phrase = next(r["relevance_score"] for r in table_results if r["table_name"] == "Power BI Reports")
    score_tokens = next(r["relevance_score"] for r in table_results if r["table_name"] == "Power Tools")
    assert score_phrase > score_tokens, (
        f"Phrase-match result ({score_phrase}) should outrank token-only result ({score_tokens})"
    )


def test_phrase_bonus_not_triggered_when_no_phrase():
    """Power Tools does not contain 'power bi' as a phrase — no phrase reason."""
    result = search_metadata("Power BI")
    tools = [r for r in result["results"] if r["asset_type"] == "table" and r["table_name"] == "Power Tools"]
    if tools:
        assert "Exact phrase match" not in tools[0]["match_reasons"]


# ---------------------------------------------------------------------------
# Integration: multi-field bonus
# ---------------------------------------------------------------------------

def test_multi_field_result_outranks_single_field_result():
    """WidgetCatalog (matches table_name + business_name + description) must outscore
    WidgetLog (matches table_name only) for the same query."""
    result = search_metadata("widget")
    tables = {r["table_name"]: r for r in result["results"] if r["asset_type"] == "table"}

    assert "Widget Catalog" in tables, f"Widget Catalog not found. Tables: {list(tables.keys())}"
    assert "Widget Log" in tables, f"Widget Log not found. Tables: {list(tables.keys())}"

    score_multi  = tables["Widget Catalog"]["relevance_score"]
    score_single = tables["Widget Log"]["relevance_score"]
    assert score_multi > score_single, (
        f"Multi-field result ({score_multi}) should outscore single-field result ({score_single})"
    )


def test_multi_field_result_has_more_reasons_than_single():
    result = search_metadata("widget")
    tables = {r["table_name"]: r for r in result["results"] if r["asset_type"] == "table"}
    catalog_reasons = tables["Widget Catalog"]["match_reasons"]
    log_reasons     = tables["Widget Log"]["match_reasons"]
    assert len(catalog_reasons) > len(log_reasons), (
        f"Catalog reasons: {catalog_reasons}  |  Log reasons: {log_reasons}"
    )


# ---------------------------------------------------------------------------
# Integration: synonym-expanded matches still produce reasons
# ---------------------------------------------------------------------------

def test_synonym_match_still_produces_match_reasons():
    """Searching 'client' (synonym of 'customer') finds Customers and has match_reasons."""
    result = search_metadata("client")
    customers = [
        r for r in result["results"]
        if r["asset_type"] == "table" and "customer" in r["table_name"].lower()
    ]
    assert len(customers) > 0, "Expected Customers table via 'client' synonym"
    assert len(customers[0]["match_reasons"]) > 0
