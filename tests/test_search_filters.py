"""
Tests for Change 3 — server-side filters, filter endpoint, and navigation targets.

Covers:
  - get_search_filters() returns correct distinct values
  - server-side schema / domain / entity / pii / classification / profile_status /
    dictionary_status / semantic_type filters all narrow results correctly
  - nav_target structure: type, tab, schema, table_fqn, column_name, IDs
  - Existing test: nav_target.source_id still present (backward compat)
  - confidence and profiled_at fields present in results

Run from project root:
    python -m pytest tests/test_search_filters.py -v
"""

import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",     "test-jwt-secret-filters")
os.environ.setdefault("USER_ID_SALT",   "test-salt-filters")

import data.search_service as search_service_module
from data.search_service import (
    get_search_filters,
    search_metadata,
)

# ---------------------------------------------------------------------------
# Seeded in-memory DB
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
        updated_at TEXT NOT NULL DEFAULT '2026-06-27'
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
        updated_at TEXT NOT NULL DEFAULT '2026-06-27'
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

    # Tables across two schemas
    conn.execute("INSERT INTO profiling_table_profiles "
                 "(source_id, table_fqn, table_name, schema_name, table_class, pii_column_count, profiling_status) "
                 "VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'TRANSACTION', 0, 'COMPLETE')")
    conn.execute("INSERT INTO profiling_table_profiles "
                 "(source_id, table_fqn, table_name, schema_name, table_class, pii_column_count, profiling_status) "
                 "VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'MASTER_DATA', 2, 'COMPLETE')")
    conn.execute("INSERT INTO profiling_table_profiles "
                 "(source_id, table_fqn, table_name, schema_name, table_class, pii_column_count, profiling_status) "
                 "VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'TRANSACTION', 0, 'COMPLETE')")
    conn.execute("INSERT INTO profiling_table_profiles "
                 "(source_id, table_fqn, table_name, schema_name, table_class, pii_column_count, profiling_status) "
                 "VALUES (2, 'fin.Customers', 'Customers', 'fin', 'MASTER_DATA', 1, 'STRUCTURAL_ONLY')")

    # Columns
    conn.execute("INSERT INTO profiling_column_profiles "
                 "(source_id, table_fqn, column_name, semantic_type, pii_confirmed) "
                 "VALUES (1, 'dbo.Employees', 'EmailAddress', 'EMAIL', 1)")
    conn.execute("INSERT INTO profiling_column_profiles "
                 "(source_id, table_fqn, column_name, semantic_type, pii_confirmed) "
                 "VALUES (1, 'dbo.Employees', 'EmployeeID', 'IDENTIFIER', 0)")
    conn.execute("INSERT INTO profiling_column_profiles "
                 "(source_id, table_fqn, column_name, semantic_type, pii_confirmed) "
                 "VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'CURRENCY', 0)")
    conn.execute("INSERT INTO profiling_column_profiles "
                 "(source_id, table_fqn, column_name, semantic_type, pii_confirmed) "
                 "VALUES (1, 'dbo.Projects', 'ProjectName', 'TEXT', 0)")

    # Dictionary tables
    conn.execute("INSERT INTO data_dictionary_tables "
                 "(source_id, table_fqn, table_name, schema_name, business_name, description, domain) "
                 "VALUES (1, 'dbo.Projects', 'Projects', 'dbo', 'Project Registry', 'Tracks all active and historical projects', 'Project Management')")
    conn.execute("INSERT INTO data_dictionary_tables "
                 "(source_id, table_fqn, table_name, schema_name, business_name, description, domain, is_approved) "
                 "VALUES (1, 'dbo.Employees', 'Employees', 'dbo', 'Employee Master', 'Contains all employee records including PII', 'HR', 1)")
    conn.execute("INSERT INTO data_dictionary_tables "
                 "(source_id, table_fqn, table_name, schema_name, business_name, description, domain) "
                 "VALUES (2, 'fin.Invoices', 'Invoices', 'fin', 'Invoice Register', 'All finance invoices and billing records', 'Finance')")

    # Dictionary columns
    conn.execute("INSERT INTO data_dictionary_columns "
                 "(source_id, table_fqn, column_name, business_label, meaning, semantic_type, pii_risk) "
                 "VALUES (1, 'dbo.Employees', 'EmailAddress', 'Employee Email', 'Primary contact email address', 'EMAIL', 1)")
    conn.execute("INSERT INTO data_dictionary_columns "
                 "(source_id, table_fqn, column_name, business_label, meaning) "
                 "VALUES (1, 'dbo.Projects', 'ProjectName', 'Project Title', 'Official name of the project')")
    conn.execute("INSERT INTO data_dictionary_columns "
                 "(source_id, table_fqn, column_name, business_label, meaning, semantic_type) "
                 "VALUES (2, 'fin.Invoices', 'InvoiceAmount', 'Invoice Amount', 'Total amount billed', 'CURRENCY')")

    # Domain assignments
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence) "
                 "VALUES (1, 'dbo.Projects', 'Project Management', 0.9)")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence) "
                 "VALUES (1, 'dbo.Employees', 'Human Resources', 0.85)")
    conn.execute("INSERT INTO domain_assignments (source_id, table_fqn, domain, confidence) "
                 "VALUES (2, 'fin.Invoices', 'Finance', 0.95)")

    # Entity assignments
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity, confidence) "
                 "VALUES (1, 'dbo.Projects', 'Project', 0.9)")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity, confidence) "
                 "VALUES (1, 'dbo.Employees', 'Employee', 0.88)")
    conn.execute("INSERT INTO entity_assignments (source_id, table_fqn, entity, confidence) "
                 "VALUES (2, 'fin.Invoices', 'Invoice', 0.92)")

    conn.commit()


@pytest.fixture(autouse=True)
def patch_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_filters.db"
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
# get_search_filters() unit tests
# ---------------------------------------------------------------------------

def test_filters_returns_sources():
    f = get_search_filters()
    ids = [s["id"] for s in f["sources"]]
    assert 1 in ids and 2 in ids


def test_filters_sources_have_name():
    f = get_search_filters()
    for s in f["sources"]:
        assert "id" in s and "name" in s
        assert s["name"]


def test_filters_returns_schemas():
    f = get_search_filters()
    assert "dbo" in f["schemas"]
    assert "fin" in f["schemas"]


def test_filters_returns_domains():
    f = get_search_filters()
    domains = f["domains"]
    assert "Finance" in domains
    assert "Human Resources" in domains
    assert "Project Management" in domains


def test_filters_returns_entities():
    f = get_search_filters()
    assert "Project" in f["entities"]
    assert "Employee" in f["entities"]
    assert "Invoice" in f["entities"]


def test_filters_returns_semantic_types():
    f = get_search_filters()
    # EMAIL appears in both profiling_column_profiles and data_dictionary_columns
    assert "EMAIL" in f["semantic_types"]
    assert "CURRENCY" in f["semantic_types"]


def test_filters_returns_classifications():
    f = get_search_filters()
    assert "TRANSACTION" in f["classifications"]
    assert "MASTER_DATA" in f["classifications"]


def test_filters_returns_profile_statuses():
    f = get_search_filters()
    assert "COMPLETE" in f["profile_statuses"]
    assert "STRUCTURAL_ONLY" in f["profile_statuses"]


def test_filters_pii_available_when_pii_exists():
    f = get_search_filters()
    assert f["pii_available"] is True


def test_filters_returns_dictionary_statuses_approved():
    f = get_search_filters()
    # Employees dict entry is approved
    assert "approved" in f["dictionary_statuses"]


def test_filters_returns_dictionary_statuses_generated():
    f = get_search_filters()
    # Projects and Invoices dict entries are generated (not approved)
    assert "generated" in f["dictionary_statuses"]


# ---------------------------------------------------------------------------
# Server-side filter integration tests — search_metadata
# ---------------------------------------------------------------------------

def test_filter_by_schema_dbo():
    result = search_metadata("employee", schema="dbo")
    for r in result["results"]:
        assert r["schema_name"] == "dbo", f"Expected schema 'dbo', got '{r['schema_name']}'"


def test_filter_by_schema_fin():
    result = search_metadata("invoice", schema="fin")
    for r in result["results"]:
        assert r["schema_name"] == "fin"


def test_filter_by_domain():
    result = search_metadata("employee", domain="Human Resources")
    assert result["total"] > 0
    for r in result["results"]:
        assert "Human Resources" in (r["domain"] or ""), (
            f"Result domain '{r['domain']}' does not match filter 'Human Resources'"
        )


def test_filter_by_entity():
    result = search_metadata("invoice", entity="Invoice")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["entity"] == "Invoice"


def test_filter_by_pii_true():
    result = search_metadata("employee", pii=True)
    assert result["total"] > 0
    for r in result["results"]:
        assert r["pii_indicator"] is True, f"Expected PII result, got non-PII: {r['display_name']}"


def test_filter_by_semantic_type():
    result = search_metadata("email", semantic_type="EMAIL")
    col_results = [r for r in result["results"] if r["asset_type"] == "column"]
    assert len(col_results) > 0
    for r in col_results:
        assert r["semantic_type"] == "EMAIL"


def test_filter_by_classification_transaction():
    result = search_metadata("invoice", classification="TRANSACTION")
    assert result["total"] > 0
    # Table results carry classification in semantic_type; column results store
    # their own semantic_type (e.g. CURRENCY) — filter still restricts parent table_class.
    table_results = [r for r in result["results"] if r["asset_type"] == "table"]
    assert len(table_results) > 0, "Expected at least one table result for TRANSACTION filter"
    for r in table_results:
        assert r["semantic_type"] == "TRANSACTION", (
            f"Table semantic_type should equal classification, got: {r['semantic_type']}"
        )


def test_filter_by_classification_master_data():
    result = search_metadata("employee", classification="MASTER_DATA")
    assert result["total"] > 0
    table_results = [r for r in result["results"] if r["asset_type"] == "table"]
    assert len(table_results) > 0, "Expected at least one table result for MASTER_DATA filter"
    for r in table_results:
        assert r["semantic_type"] == "MASTER_DATA", (
            f"Table semantic_type should equal classification, got: {r['semantic_type']}"
        )


def test_filter_by_profile_status():
    result = search_metadata("customer", profile_status="STRUCTURAL_ONLY")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["profiling_status"] == "STRUCTURAL_ONLY"


def test_filter_by_dictionary_status_approved():
    result = search_metadata("employee", dictionary_status="approved")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["dictionary_status"] == "approved"


def test_filter_by_dictionary_status_generated():
    result = search_metadata("invoice", dictionary_status="generated")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["dictionary_status"] == "generated"


def test_combined_filters_narrow_results():
    """Multiple filters applied together should return fewer results than no filters."""
    all_results = search_metadata("e", limit=100, offset=0)
    filtered    = search_metadata("e", schema="dbo", limit=100, offset=0)
    # All filtered results must be in dbo schema
    for r in filtered["results"]:
        assert r["schema_name"] == "dbo"
    # Filtered set should be no larger than total set
    assert filtered["total"] <= all_results["total"]


# ---------------------------------------------------------------------------
# Navigation target structure tests
# ---------------------------------------------------------------------------

def test_table_nav_target_type_is_table():
    result = search_metadata("Projects", asset_type="table")
    for r in result["results"]:
        assert r["nav_target"]["type"] == "table"


def test_table_nav_target_tab_is_schema():
    result = search_metadata("Projects", asset_type="table")
    for r in result["results"]:
        assert r["nav_target"]["tab"] == "schema"


def test_table_nav_target_has_schema_and_table_fqn():
    result = search_metadata("Projects", asset_type="table")
    assert result["total"] > 0
    nav = result["results"][0]["nav_target"]
    assert "schema"    in nav
    assert "table_fqn" in nav
    assert nav["table_fqn"]


def test_column_nav_target_type_is_column():
    result = search_metadata("EmailAddress", asset_type="column")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["nav_target"]["type"] == "column"


def test_column_nav_target_tab_is_profile():
    result = search_metadata("EmailAddress", asset_type="column")
    assert result["total"] > 0
    for r in result["results"]:
        assert r["nav_target"]["tab"] == "profile"


def test_column_nav_target_has_column_name():
    result = search_metadata("EmailAddress", asset_type="column")
    assert result["total"] > 0
    nav = result["results"][0]["nav_target"]
    assert "column_name" in nav
    assert nav["column_name"] == "EmailAddress"


def test_nav_target_source_id_present_backward_compat():
    """Existing assertion: nav_target.source_id must still be non-None."""
    result = search_metadata("Projects")
    for r in result["results"]:
        assert r["nav_target"]["source_id"] is not None


def test_domain_filter_nav_target_is_domain_type():
    result = search_metadata("Project", asset_type="domain")
    assert result["total"] > 0
    # At least one result should have domain nav_target (when domain_id is set)
    domain_navs = [r for r in result["results"] if r["nav_target"]["type"] == "domain"]
    assert len(domain_navs) > 0, "Expected at least one domain nav_target"


def test_entity_filter_nav_target_is_entity_type():
    result = search_metadata("Project", asset_type="entity")
    assert result["total"] > 0
    entity_navs = [r for r in result["results"] if r["nav_target"]["type"] == "entity"]
    assert len(entity_navs) > 0, "Expected at least one entity nav_target"


def test_dictionary_filter_table_nav_target_is_dictionary_type():
    result = search_metadata("Employee", asset_type="dictionary")
    table_results = [r for r in result["results"] if r["asset_type"] == "table"]
    dict_navs = [r for r in table_results if r["nav_target"]["type"] == "dictionary"]
    assert len(dict_navs) > 0, "Expected dictionary nav_target for dict-filtered table results"


# ---------------------------------------------------------------------------
# New result fields: confidence and profiled_at
# ---------------------------------------------------------------------------

def test_confidence_field_present_in_every_result():
    result = search_metadata("Projects")
    assert result["total"] > 0
    for r in result["results"]:
        assert "confidence" in r, f"'confidence' missing from result: {r['display_name']}"
        assert isinstance(r["confidence"], float)


def test_confidence_nonzero_when_assignments_exist():
    result = search_metadata("Projects")
    # Projects has domain (0.9) and entity (0.9) assignments
    proj = [r for r in result["results"] if r["asset_type"] == "table" and "Project" in r["display_name"]]
    assert len(proj) > 0
    assert proj[0]["confidence"] > 0


def test_profiled_at_field_present():
    result = search_metadata("Projects")
    assert result["total"] > 0
    for r in result["results"]:
        assert "profiled_at" in r


def test_profiling_status_field_present():
    result = search_metadata("customer")
    for r in result["results"]:
        assert "profiling_status" in r
