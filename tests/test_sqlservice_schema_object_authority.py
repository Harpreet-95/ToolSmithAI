"""
Focused tests — Phase 7, Step 3E, Part 8: automatic schema-object authority
classification (core/sqlservice/schema_object_authority.py).

No real OpenAI/embedding call anywhere in this file — this module never
makes one. Documents are hand-built fake objects (object_fqn/object_type/
retrieval_tier/metadata only, the exact shape compute_schema_object_
authority reads) rather than a real vector index — no embedding pipeline
is needed to exercise this module directly. semantic_entity_contracts/
view_semantic_evidence rows are inserted directly into an isolated,
per-test temporary SQLite database (never the real one), reusing the same
template-schema fixture plumbing as tests/test_sqlservice_schema_frontier.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pytest

import core.sqlservice.schema_object_authority as authority_module
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_CANONICAL_TABLE,
    AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW,
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_INCOMPATIBLE_GRAIN,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
    AUTHORITY_CLASS_RESTRICTED_VIEW,
    AUTHORITY_CLASS_UNKNOWN,
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
    GRAIN_CLASS_AGGREGATED,
    GRAIN_CLASS_DEDUPLICATED,
    ROW_PRESERVATION_AT_RISK,
    ROW_PRESERVATION_PRESERVED,
    SchemaObjectAuthorityIdentity,
    clear_schema_object_authority_cache,
    compute_schema_object_authority,
    load_schema_object_authority,
    schema_object_authority_cache_size,
)

from tests.sqlservice_fixtures import template_db_path  # noqa: F401

SOURCE_ID = 90001
BUILD_ID = 1


def _row_connection(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def db_path(template_db_path, tmp_path, monkeypatch):
    import shutil

    path = str(tmp_path / "authority_fixture.db")
    shutil.copy(template_db_path, path)
    monkeypatch.setattr(authority_module, "get_connection", lambda: _row_connection(path))
    return path


@pytest.fixture(autouse=True)
def _isolated_authority_cache():
    clear_schema_object_authority_cache()
    yield
    clear_schema_object_authority_cache()


@dataclass(frozen=True)
class _FakeDoc:
    object_fqn: str
    object_type: str
    retrieval_tier: int
    metadata: dict = field(default_factory=dict)


def _insert_contract(
    db_path, entity_name, *, source_id=SOURCE_ID, canonical_table_fqn=None, preferred_view_fqn=None,
    resolution_status="RESOLVED", contract_version=1,
):
    conn = sqlite3.connect(db_path)
    now = "2026-09-24T00:00:00Z"
    conn.execute(
        "INSERT INTO semantic_entity_contracts "
        "(source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, "
        " resolution_status, confidence, contract_json, metadata_revision, generation_method, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1.0, '{}', 'rev1', 'auto_grounding', ?, ?)",
        (source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, resolution_status, now, now),
    )
    conn.commit()
    conn.close()


def _insert_view_evidence(db_path, table_fqn, definition_sql, referenced_tables, *, source_id=SOURCE_ID):
    import json

    conn = sqlite3.connect(db_path)
    now = "2026-09-24T00:00:00Z"
    conn.execute(
        "INSERT INTO view_semantic_evidence "
        "(source_id, snapshot_id, table_fqn, definition_sql, definition_checksum, referenced_tables_json, "
        " restriction_json, naming_penalty_score, is_curated, discovered_at) "
        "VALUES (?, 1, ?, ?, 'x', ?, NULL, 0.0, 1, ?)",
        (source_id, table_fqn, definition_sql, json.dumps(referenced_tables), now),
    )
    conn.commit()
    conn.close()


def _identity(source_id=SOURCE_ID, build_id=BUILD_ID):
    return SchemaObjectAuthorityIdentity(
        build_id=build_id, source_id=source_id, schema_fingerprint="fp",
        authority_state=authority_module._authority_state(source_id),
    )


# ── 1. Canonical table from trusted semantic contract ──────────────────────

# ── Phase 7, Step 6E3B5, Part 3 -- _table_fqn's schema-omission default ──


def test_unqualified_table_reference_defaults_to_dbo_schema():
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one("SELECT x FROM SomeTable t", read="tsql")
    table = next(n for n in tree.walk() if isinstance((n[0] if isinstance(n, tuple) else n), exp.Table))
    table = table[0] if isinstance(table, tuple) else table
    assert authority_module._table_fqn(table) == "dbo.SomeTable"


def test_schema_qualified_table_reference_unaffected():
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one("SELECT x FROM dnnuser.SomeTable t", read="tsql")
    table = next(n for n in tree.walk() if isinstance((n[0] if isinstance(n, tuple) else n), exp.Table))
    table = table[0] if isinstance(table, tuple) else table
    assert authority_module._table_fqn(table) == "dnnuser.SomeTable"


def test_canonical_table_from_trusted_contract(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1)]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.widget"]
    assert a.authority_class == AUTHORITY_CLASS_CANONICAL_TABLE
    assert a.canonical_table_fqns == ("Widget",)


def test_table_without_contract_is_unknown_not_canonical(db_path):
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1)]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.widget"]
    assert a.authority_class == AUTHORITY_CLASS_UNKNOWN


def test_tier34_table_is_unsafe_dependency(db_path):
    docs = [_FakeDoc("dbo.Widget_backup", "TABLE", 4)]
    result = compute_schema_object_authority(docs, _identity())
    assert result.authorities_by_fqn_lower["dbo.widget_backup"].authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY


# ── 2. Declared preferred view ──────────────────────────────────────────────

def test_declared_preferred_view_wins_outright(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget", preferred_view_fqn="dbo.vw_Widget")
    # Deliberately give the view a definition that WOULD otherwise fail
    # (a restrictive WHERE) -- declared authority must win regardless.
    _insert_view_evidence(db_path, "dbo.vw_Widget", "CREATE VIEW v AS SELECT * FROM dbo.Widget WHERE Region = 'X'", ["dbo.Widget"])
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_Widget", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.vw_widget"]
    assert a.authority_class == AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW
    assert a.preferred_view_for_entities == ("Widget",)


# ── 3. Unfiltered PK-preserving equivalent view ─────────────────────────────

def test_unfiltered_left_join_view_is_proven_equivalent(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetOverview",
        "CREATE VIEW v AS SELECT a.WidgetID, a.Name, b.CategoryName FROM dbo.Widget a "
        "LEFT JOIN dbo.Category b ON a.CategoryID = b.CategoryID",
        ["dbo.Widget", "dbo.Category"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1, {"primary_key_columns": ["WidgetID"]}),
        _FakeDoc("dbo.Category", "TABLE", 1),
        _FakeDoc("dbo.vw_WidgetOverview", "VIEW", 1, {"column_names": ["WidgetID", "Name", "CategoryName"]}),
    ]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.vw_widgetoverview"]
    assert a.authority_class == AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW
    assert a.canonical_table_fqns == ("Widget",)
    assert a.row_preservation_status == ROW_PRESERVATION_PRESERVED


# ── 4/5. Filtered view / hardcoded literal restriction ──────────────────────

def test_where_literal_filter_classified_restricted(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_ActiveWidget",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a WHERE a.RegionID = 7",
        ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_ActiveWidget", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.vw_activewidget"]
    assert a.authority_class == AUTHORITY_CLASS_RESTRICTED_VIEW
    assert "where_or_having_literal_filter" in a.restrictions


def test_hardcoded_literal_projected_column_classified_restricted(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetFakeCategory",
        "CREATE VIEW v AS SELECT a.WidgetID, 'Standard' AS Category FROM dbo.Widget a",
        ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_WidgetFakeCategory", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.vw_widgetfakecategory"]
    assert a.authority_class == AUTHORITY_CLASS_RESTRICTED_VIEW
    assert "hardcoded_literal_projected_column" in a.restrictions
    assert any("Category" in e for e in a.evidence)


# ── 6. GROUP BY/aggregate incompatible grain ────────────────────────────────

def test_group_by_view_is_incompatible_grain(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetMonthly",
        "CREATE VIEW v AS SELECT a.CategoryID, count(*) Cnt FROM dbo.Widget a GROUP BY a.CategoryID",
        ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_WidgetMonthly", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())

    a = result.authorities_by_fqn_lower["dbo.vw_widgetmonthly"]
    assert a.authority_class == AUTHORITY_CLASS_INCOMPATIBLE_GRAIN
    assert a.grain_class == GRAIN_CLASS_AGGREGATED
    assert "GROUP_BY" in a.aggregation_status


def test_bare_root_aggregate_without_group_by_is_incompatible_grain(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetCount", "CREATE VIEW v AS SELECT count(*) Cnt FROM dbo.Widget a", ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_WidgetCount", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())
    assert result.authorities_by_fqn_lower["dbo.vw_widgetcount"].authority_class == AUTHORITY_CLASS_INCOMPATIBLE_GRAIN


def test_nested_subquery_aggregate_does_not_count_as_root_grain(db_path):
    """A scalar-subquery aggregate does not change the OUTER query's own
    row grain -- must not be misclassified as INCOMPATIBLE_GRAIN."""
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetWithCount",
        "CREATE VIEW v AS SELECT a.WidgetID, (SELECT COUNT(*) FROM dbo.Order o WHERE o.WidgetID = a.WidgetID) AS OrderCount "
        "FROM dbo.Widget a",
        ["dbo.Widget", "dbo.Order"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1, {"primary_key_columns": ["WidgetID"]}), _FakeDoc("dbo.Order", "TABLE", 1),
        _FakeDoc("dbo.vw_WidgetWithCount", "VIEW", 1, {"column_names": ["WidgetID", "OrderCount"]}),
    ]

    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_widgetwithcount"]
    assert a.authority_class != AUTHORITY_CLASS_INCOMPATIBLE_GRAIN
    assert a.authority_class == AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW


# ── 7. DISTINCT behavior ─────────────────────────────────────────────────

def test_distinct_view_is_conditionally_compatible(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_DistinctWidget",
        "CREATE VIEW v AS SELECT DISTINCT a.CategoryID FROM dbo.Widget a", ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_DistinctWidget", "VIEW", 1)]

    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_distinctwidget"]
    assert a.authority_class == AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW
    assert a.grain_class == GRAIN_CLASS_DEDUPLICATED


# ── 8. Inner-join row-loss risk ──────────────────────────────────────────

def test_inner_join_to_non_anchor_dependency_is_at_risk(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetWithVendor",
        "CREATE VIEW v AS SELECT a.WidgetID, b.VendorName FROM dbo.Widget a "
        "INNER JOIN dbo.Vendor b ON a.VendorID = b.VendorID",
        ["dbo.Widget", "dbo.Vendor"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1, {"primary_key_columns": ["WidgetID"]}), _FakeDoc("dbo.Vendor", "TABLE", 1),
        _FakeDoc("dbo.vw_WidgetWithVendor", "VIEW", 1, {"column_names": ["WidgetID", "VendorName"]}),
    ]

    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_widgetwithvendor"]
    assert a.authority_class == AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW
    assert a.row_preservation_status == ROW_PRESERVATION_AT_RISK
    assert any("dbo.vendor" in e for e in a.evidence)


# ── 9. LEFT JOIN lookup enrichment preserves canonical grain (same as #3,
#      restated explicitly per Part 8's own list) ─────────────────────────

def test_left_join_lookup_enrichment_preserves_grain(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetEnriched",
        "CREATE VIEW v AS SELECT a.WidgetID, b.VendorName FROM dbo.Widget a "
        "LEFT JOIN dbo.Vendor b ON a.VendorID = b.VendorID",
        ["dbo.Widget", "dbo.Vendor"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1, {"primary_key_columns": ["WidgetID"]}), _FakeDoc("dbo.Vendor", "TABLE", 1),
        _FakeDoc("dbo.vw_WidgetEnriched", "VIEW", 1, {"column_names": ["WidgetID", "VendorName"]}),
    ]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_widgetenriched"]
    assert a.authority_class == AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW
    assert a.row_preservation_status == ROW_PRESERVATION_PRESERVED


# ── 10. UNION/set-operation behavior ─────────────────────────────────────

def test_union_view_with_tier4_branch_dependency_is_unsafe(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_AllWidgets",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a UNION SELECT b.WidgetID FROM dbo.ArchivedWidget b",
        ["dbo.Widget", "dbo.ArchivedWidget"],
    )
    docs = [
        # This module trusts the CALLER-supplied retrieval_tier (already
        # computed once, elsewhere, by core.sqlservice.retrieval_documents.
        # classify_retrieval_tier) — it never re-derives tier from a name
        # itself. Tier 4 here is what a real archive-table classification
        # would already have produced upstream.
        _FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.ArchivedWidget", "TABLE", 4),
        _FakeDoc("dbo.vw_AllWidgets", "VIEW", 1),
    ]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_allwidgets"]
    assert a.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY


def test_union_view_without_unsafe_dependency_is_conditionally_compatible(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_AllWidgets",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a UNION SELECT b.WidgetID FROM dbo.OtherWidget b",
        ["dbo.Widget", "dbo.OtherWidget"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.OtherWidget", "TABLE", 1),
        _FakeDoc("dbo.vw_AllWidgets", "VIEW", 1),
    ]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_allwidgets"]
    assert a.authority_class == AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW
    assert "set_operation" in a.evidence


# ── 11/12. Tier-3/4, archive/backup/system dependency ───────────────────

def test_tier4_dependency_makes_view_unsafe(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetHistory",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a LEFT JOIN dbo.Widget_backup b ON a.WidgetID = b.WidgetID",
        ["dbo.Widget", "dbo.Widget_backup"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.Widget_backup", "TABLE", 4),
        _FakeDoc("dbo.vw_WidgetHistory", "VIEW", 1),
    ]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_widgethistory"]
    assert a.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    assert "dbo.widget_backup" in a.unsafe_dependency_fqns


def test_system_schema_dependency_makes_view_unsafe(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetUsers",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a LEFT JOIN sys.Users b ON a.OwnerID = b.UserID",
        ["dbo.Widget", "sys.Users"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("sys.Users", "TABLE", 3),
        _FakeDoc("dbo.vw_WidgetUsers", "VIEW", 1),
    ]
    result = compute_schema_object_authority(docs, _identity())
    assert result.authorities_by_fqn_lower["dbo.vw_widgetusers"].authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY


def test_transitive_unsafe_dependency_through_another_view(db_path):
    """AST analysis never invents a dependency -- but the closure walk over
    ALREADY-STORED referenced_tables must still reach a second-hop unsafe
    object through an intermediate VIEW."""
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetBase", "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a", ["dbo.Widget"],
    )
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetLayered",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.vw_WidgetBase a LEFT JOIN dbo.Widget_archive b ON a.WidgetID = b.WidgetID",
        ["dbo.vw_WidgetBase", "dbo.Widget_archive"],
    )
    docs = [
        _FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.Widget_archive", "TABLE", 4),
        _FakeDoc("dbo.vw_WidgetBase", "VIEW", 1), _FakeDoc("dbo.vw_WidgetLayered", "VIEW", 1),
    ]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_widgetlayered"]
    assert a.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    assert "dbo.widget_archive" in a.unsafe_dependency_fqns


# ── 13. Missing/unparseable definition fails closed ─────────────────────

def test_missing_view_definition_is_unknown(db_path):
    docs = [_FakeDoc("dbo.vw_NoEvidence", "VIEW", 1)]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_noevidence"]
    assert a.authority_class == AUTHORITY_CLASS_UNKNOWN
    assert a.warnings


def test_unparseable_definition_fails_closed_to_unknown(db_path):
    _insert_view_evidence(db_path, "dbo.vw_Broken", "THIS IS NOT VALID SQL {{{", [])
    docs = [_FakeDoc("dbo.vw_Broken", "VIEW", 1)]
    result = compute_schema_object_authority(docs, _identity())
    a = result.authorities_by_fqn_lower["dbo.vw_broken"]
    assert a.authority_class == AUTHORITY_CLASS_UNKNOWN
    assert a.warnings


def test_non_select_statement_fails_closed_to_unknown(db_path):
    _insert_view_evidence(db_path, "dbo.vw_Weird", "DELETE FROM dbo.Widget", [])
    docs = [_FakeDoc("dbo.vw_Weird", "VIEW", 1)]
    result = compute_schema_object_authority(docs, _identity())
    assert result.authorities_by_fqn_lower["dbo.vw_weird"].authority_class == AUTHORITY_CLASS_UNKNOWN


# ── 14. Deterministic evidence and cache identity ────────────────────────

def test_deterministic_under_repeated_computation(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_WidgetOverview",
        "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a WHERE a.RegionID = 1", ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_WidgetOverview", "VIEW", 1)]

    r1 = compute_schema_object_authority(docs, _identity())
    r2 = compute_schema_object_authority(list(reversed(docs)), _identity())
    assert r1.authorities_by_fqn_lower["dbo.vw_widgetoverview"] == r2.authorities_by_fqn_lower["dbo.vw_widgetoverview"]


def test_load_uses_cache_on_second_call(db_path):
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1)]
    assert schema_object_authority_cache_size() == 0
    r1 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert schema_object_authority_cache_size() == 1
    r2 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert r1 is r2
    assert schema_object_authority_cache_size() == 1


# ── 15. Cache invalidation after definition/contract change ─────────────

def test_cache_invalidates_after_new_contract(db_path):
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1)]
    r1 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert r1.authorities_by_fqn_lower["dbo.widget"].authority_class == AUTHORITY_CLASS_UNKNOWN

    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    r2 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert r2 is not r1
    assert r2.authorities_by_fqn_lower["dbo.widget"].authority_class == AUTHORITY_CLASS_CANONICAL_TABLE


def test_cache_invalidates_after_view_definition_change(db_path):
    _insert_contract(db_path, "Widget", canonical_table_fqn="dbo.Widget")
    _insert_view_evidence(
        db_path, "dbo.vw_Widget", "CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a", ["dbo.Widget"],
    )
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1), _FakeDoc("dbo.vw_Widget", "VIEW", 1)]
    r1 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert r1.authorities_by_fqn_lower["dbo.vw_widget"].authority_class == AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE view_semantic_evidence SET definition_sql = ?, discovered_at = ? WHERE table_fqn = 'dbo.vw_Widget'",
        ("CREATE VIEW v AS SELECT a.WidgetID FROM dbo.Widget a WHERE a.RegionID = 1", "2026-09-24T01:00:00Z"),
    )
    conn.commit()
    conn.close()

    r2 = load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert r2 is not r1
    assert r2.authorities_by_fqn_lower["dbo.vw_widget"].authority_class == AUTHORITY_CLASS_RESTRICTED_VIEW


def test_cache_invalidates_on_build_id_change(db_path):
    docs = [_FakeDoc("dbo.Widget", "TABLE", 1)]
    load_schema_object_authority(docs, BUILD_ID, SOURCE_ID, "fp")
    assert schema_object_authority_cache_size() == 1
    load_schema_object_authority(docs, BUILD_ID + 1, SOURCE_ID, "fp2")
    assert schema_object_authority_cache_size() == 2


# ── 16. No database-specific literals (mirrors core.sqlservice.canonical_
#        scoring's own identical guard) ──────────────────────────────────

def test_no_domain_specific_tokens_in_production_module():
    import inspect

    source = inspect.getsource(authority_module)
    forbidden = ("edu_", "exampleco", "student", "classsignup", "surveyanswers", "master_list")
    lowered = source.lower()
    for token in forbidden:
        assert token not in lowered, f"found domain-specific token {token!r} in schema_object_authority.py"
