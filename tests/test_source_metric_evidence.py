"""
Tests for data.business_knowledge_service.get_source_metric_evidence —
Executive Report Query Foundation milestone, Part 7/8 bounded evidence.

Run:
    python -m pytest tests/test_source_metric_evidence.py -v
"""
from __future__ import annotations

from data.business_knowledge_service import get_source_metric_evidence
from tests.test_phase9_query_planning import env, _add_table, _add_column


def test_returns_empty_for_unowned_source(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    assert get_source_metric_evidence(999, "u1") == []


def test_table_with_no_flagged_columns_is_excluded(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.noise")
    _add_column(db, "dbo.noise", "SomeID", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    evidence = get_source_metric_evidence(1, "u1")
    assert evidence == []


def test_table_with_metric_dimension_date_columns_is_surfaced(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL", is_metric=True, business_label="Revenue")
    _add_column(db, "dbo.orders", "Region", data_type="VARCHAR", is_dimension=True, business_label="Region")
    _add_column(db, "dbo.orders", "OrderDate", data_type="DATETIME", is_date=True, business_label="Order Date")

    evidence = get_source_metric_evidence(1, "u1")
    assert len(evidence) == 1
    entry = evidence[0]
    assert entry["table_fqn"] == "dbo.orders"
    assert {c["column_name"] for c in entry["metric_columns"]} == {"Revenue"}
    assert {c["column_name"] for c in entry["dimension_columns"]} == {"Region"}
    assert {c["column_name"] for c in entry["date_columns"]} == {"OrderDate"}


def test_unapproved_columns_are_surfaced_but_flagged(tmp_path, monkeypatch):
    """FINAL PRE-37 MILESTONE, Part 14 — deliberate contract change:
    dictionary/table approval is no longer the Executive Report candidacy
    gate (real business tables like the reference database's own EDU_Student/EDU_Class/
    EDU_ClassSignups all have table-level dictionary_approved=0 despite
    correctly-classified columns, which the OLD is_approved=1 gate excluded
    outright). A table with real is_metric/is_dimension/is_date evidence is
    now always a candidate, ranked by data.query_planning_service.
    _score_table_authority's business-centrality scoring instead — approval
    status is still surfaced per-entry (dictionary_approved) as
    informational evidence, never as an exclusion. This replaces the old
    test_unapproved_columns_are_excluded, which encoded the now-intentionally-
    removed exclusionary behavior."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(
        db, "dbo.orders", "DraftMetric", data_type="DECIMAL", is_metric=True,
        business_label="Draft Metric", approved=False,
    )

    evidence = get_source_metric_evidence(1, "u1")
    assert len(evidence) == 1
    assert evidence[0]["table_fqn"] == "dbo.orders"
    # dictionary_approved reflects the TABLE's own approval state (a
    # separate concept from this one column's approval, which is what this
    # test is really about) -- present as a bool either way, never used to
    # exclude this table.
    assert isinstance(evidence[0]["dictionary_approved"], bool)
    assert {c["column_name"] for c in evidence[0]["metric_columns"]} == {"DraftMetric"}


def test_ranked_by_richest_evidence_first(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL", is_metric=True, business_label="Revenue")
    _add_column(db, "dbo.orders", "Cost", data_type="DECIMAL", is_metric=True, business_label="Cost")

    _add_table(db, "dbo.students")
    _add_column(db, "dbo.students", "StudentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.students", "GPA", data_type="DECIMAL", is_metric=True, business_label="GPA")

    evidence = get_source_metric_evidence(1, "u1")
    assert [e["table_fqn"] for e in evidence] == ["dbo.orders", "dbo.students"]


def test_limit_tables_is_respected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    for i in range(5):
        fqn = f"dbo.t{i}"
        _add_table(db, fqn)
        _add_column(db, fqn, "ID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
        _add_column(db, fqn, "Amount", data_type="DECIMAL", is_metric=True, business_label="Amount")

    evidence = get_source_metric_evidence(1, "u1", limit_tables=2)
    assert len(evidence) == 2
