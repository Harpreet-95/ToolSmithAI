"""
Tests for core.reporting.report_planner — Executive Report Query Foundation
milestone, Parts 7/8 (evidence-grounded AI report planning).

Run:
    python -m pytest tests/test_report_planner.py -v
"""
from __future__ import annotations

from unittest.mock import patch

from core.reporting.report_planner import (
    _deterministic_fallback_kpis, _evidence_terms, _kpi_is_grounded, _kpi_title_entity_mismatch,
    _kpi_title_status_word_unsupported, _parse_kpi_dict, find_unsupported_title_modifiers,
    plan_executive_report_kpis, repair_unsupported_title,
)
from tests.test_phase9_query_planning import env, _add_table, _add_column


_ORDERS_EVIDENCE = [{
    "table_fqn": "dbo.orders", "business_name": "Orders", "domain": "Sales", "entity": None,
    "metric_columns": [{"column_name": "Revenue", "business_label": "Revenue"}],
    "dimension_columns": [{"column_name": "Region", "business_label": "Region"}],
    "date_columns": [{"column_name": "OrderDate", "business_label": "Order Date"}],
}]


def test_deterministic_fallback_produces_grounded_generic_kpis():
    kpis = _deterministic_fallback_kpis(_ORDERS_EVIDENCE)
    titles = [k["title"] for k in kpis]
    assert "Total Orders" in titles
    assert any("Revenue" in t for t in titles)
    # Trend KPI only proposed because both a metric AND a date column exist.
    assert any(k.get("calendar_grain") == "month" for k in kpis)
    # Breakdown KPI only proposed because both a metric AND a dimension exist.
    assert any(k.get("dimension_terms") for k in kpis)


def test_deterministic_fallback_empty_evidence_is_empty():
    assert _deterministic_fallback_kpis([]) == []


def test_grounding_rejects_hallucinated_entity():
    terms = _evidence_terms(_ORDERS_EVIDENCE)
    ungrounded = {"entity_name": "unicorn sightings", "measures": [{"term": "magic"}]}
    assert _kpi_is_grounded(ungrounded, terms) is False


def test_grounding_accepts_evidence_backed_kpi():
    terms = _evidence_terms(_ORDERS_EVIDENCE)
    grounded = {"entity_name": "orders", "measures": [{"term": "revenue"}]}
    assert _kpi_is_grounded(grounded, terms) is True


def test_parse_kpi_dict_rejects_invalid_aggregation():
    raw = {
        "title": "X", "business_question": "q", "entity_name": "orders",
        "measures": [{"term": "revenue", "aggregation": "MEDIAN"}],
    }
    assert _parse_kpi_dict(raw, 0) is None


def test_parse_kpi_dict_builds_valid_spec():
    raw = {
        "title": "Total Revenue", "business_question": "What is total revenue?",
        "entity_name": "orders", "measures": [{"term": "revenue", "aggregation": "SUM"}],
        "calendar_grain": "month", "dimension_terms": ["region"],
        "visualization_hint": "line_chart", "importance": "high", "reason": "core metric",
    }
    spec = _parse_kpi_dict(raw, 3)
    assert spec is not None
    assert spec.id == "kpi_3"
    assert spec.calendar_grain == "month"
    assert spec.dimension_terms == ("region",)
    assert spec.measures[0].aggregation == "SUM"


def test_plan_executive_report_kpis_no_evidence_returns_empty_with_warning(tmp_path, monkeypatch):
    env(tmp_path, monkeypatch)
    specs, warnings = plan_executive_report_kpis(1, "u1")
    assert specs == []
    assert warnings


def test_plan_executive_report_kpis_uses_fallback_when_ai_disabled(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    with patch("core.reporting.report_planner._ai_propose_kpis", return_value=None):
        specs, warnings = plan_executive_report_kpis(1, "u1")

    assert len(specs) >= 1
    assert any("deterministic" in w.lower() for w in warnings)


def test_plan_executive_report_kpis_filters_ungrounded_ai_proposal(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.orders", "Revenue", data_type="DECIMAL", is_metric=True, business_label="Revenue")

    fake_ai_output = [
        {
            "title": "Total Revenue", "business_question": "What is total revenue?",
            "entity_name": "orders", "measures": [{"term": "revenue", "aggregation": "SUM"}],
            "visualization_hint": "kpi_card", "importance": "high", "reason": "core metric",
        },
        {
            "title": "Unicorn Sightings", "business_question": "How many unicorns?",
            "entity_name": "unicorns", "measures": [{"term": "magic_count", "aggregation": "COUNT"}],
            "visualization_hint": "kpi_card", "importance": "low", "reason": "hallucinated",
        },
    ]
    with patch("core.reporting.report_planner._ai_propose_kpis", return_value=fake_ai_output):
        specs, warnings = plan_executive_report_kpis(1, "u1")

    assert len(specs) == 1
    assert specs[0].title == "Total Revenue"
    assert any("Unicorn" in w for w in warnings)


# ---------------------------------------------------------------------------
# FINAL ENTITY/TABLE ADJUDICATION CORRECTNESS FIX milestone — live-confirmed
# root cause of "Count of Alumni Referral Contacts" = 41,000: THIS module's
# own KPI-proposal AI call wrote a specific title while setting entity_name
# to the generic entity-classification word "User", which
# attempt_structured_kpi_plan then correctly (but wrongly, given the input)
# resolved to this source's strongest literal "User" table
# (dnnuser.Users) — never a candidate_adjudicator defect (see the milestone
# report: dnnuser.Users isn't even in the candidate pool for the CORRECT
# term "alumni referral contact"). _kpi_title_entity_mismatch is a pure
# internal-consistency check between two AI-authored fields, never a table
# selector.
# ---------------------------------------------------------------------------

def test_title_entity_mismatch_reproduces_alumni_referral_defect():
    # The exact captured shape from the live defect.
    bad = {
        "title": "Count of Alumni Referral Contacts", "entity_name": "User",
        "measures": [{"term": "User", "aggregation": "COUNT"}],
    }
    assert _kpi_title_entity_mismatch(bad) is True


def test_title_entity_mismatch_allows_genuine_agreement():
    # Title and entity_name are both plainly about the same "user" concept
    # — must NOT be treated as a mismatch merely because it's a generic word.
    good = {
        "title": "Count of Users", "entity_name": "User",
        "measures": [{"term": "User", "aggregation": "COUNT"}],
    }
    assert _kpi_title_entity_mismatch(good) is False


def test_title_entity_mismatch_allows_plural_and_synonym_variance():
    good = {
        "title": "Total Payments", "entity_name": "Payment",
        "measures": [{"term": "Amount", "aggregation": "SUM"}],
    }
    assert _kpi_title_entity_mismatch(good) is False


def test_title_entity_mismatch_checks_dimension_terms_too():
    good = {
        "title": "Average Course Fee by Program", "entity_name": "Class",
        "measures": [{"term": "course fee", "aggregation": "AVG"}],
        "dimension_terms": ["program"],
    }
    assert _kpi_title_entity_mismatch(good) is False


def test_title_entity_mismatch_empty_title_never_blocks():
    assert _kpi_title_entity_mismatch({"title": "", "entity_name": "User"}) is False
    assert _kpi_title_entity_mismatch({"title": "Total Count", "entity_name": "User"}) is False


def test_plan_executive_report_kpis_rejects_title_entity_mismatch(tmp_path, monkeypatch):
    # Non-database-specific adversarial reproduction (Part 11): a generic,
    # globally-authoritative "system_accounts" table vs a specific
    # "customer_referral_leads" table the AI's own proposal names in its
    # title but grounds to the generic table instead — the generic table
    # must NOT be allowed to answer under the specific title.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.system_accounts")
    _add_column(db, "dbo.system_accounts", "AccountID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.system_accounts", "Balance", data_type="DECIMAL", is_metric=True, business_label="Balance")
    _add_table(db, "dbo.customer_referral_leads")
    _add_column(db, "dbo.customer_referral_leads", "LeadID", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    fake_ai_output = [
        {
            "title": "Total Accounts", "business_question": "How many accounts are there?",
            "entity_name": "accounts", "measures": [{"term": "accounts", "aggregation": "COUNT"}],
            "visualization_hint": "kpi_card", "importance": "medium", "reason": "x",
        },
        {
            # The adversarial case: title names a specific business concept
            # ("Customer Referral Leads") while entity_name/measure grounds
            # to the unrelated generic "accounts" concept.
            "title": "Count of Customer Referral Leads", "business_question": "How many referral leads?",
            "entity_name": "accounts", "measures": [{"term": "accounts", "aggregation": "COUNT"}],
            "visualization_hint": "kpi_card", "importance": "medium", "reason": "x",
        },
    ]
    with patch("core.reporting.report_planner._ai_propose_kpis", return_value=fake_ai_output):
        specs, warnings = plan_executive_report_kpis(1, "u1")

    titles = [s.title for s in specs]
    assert "Total Accounts" in titles
    assert "Count of Customer Referral Leads" not in titles
    assert any("Customer Referral Leads" in w and "rejected" in w for w in warnings)


# ---------------------------------------------------------------------------
# NEXT MILESTONE, Part 19 — title claims a status condition its own
# business_question never asks for ("Active Users" whose business_question
# is a plain, unfiltered total).
# ---------------------------------------------------------------------------

def test_title_status_word_unsupported_detects_mismatch():
    raw = {"title": "Active Users", "business_question": "How many users are there?"}
    assert _kpi_title_status_word_unsupported(raw) == "Active"


def test_title_status_word_supported_when_question_also_asks_for_it():
    raw = {"title": "Active Users", "business_question": "How many active users are there?"}
    assert _kpi_title_status_word_unsupported(raw) is None


def test_title_status_word_none_when_title_has_no_status_word():
    raw = {"title": "Total Users", "business_question": "How many users are there?"}
    assert _kpi_title_status_word_unsupported(raw) is None


def test_plan_executive_report_kpis_renames_unsupported_status_title(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.users")
    _add_column(db, "dbo.users", "UserID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.users", "SignupCount", data_type="INTEGER", is_metric=True, business_label="Signup Count")

    fake_ai_output = [{
        "title": "Active Users", "business_question": "How many users are there?",
        "entity_name": "users", "measures": [{"term": "users", "aggregation": "COUNT"}],
        "visualization_hint": "kpi_card", "importance": "medium", "reason": "x",
    }]
    with patch("core.reporting.report_planner._ai_propose_kpis", return_value=fake_ai_output):
        specs, warnings = plan_executive_report_kpis(1, "u1")

    titles = [s.title for s in specs]
    assert "Active Users" not in titles
    assert "Users" in titles  # "Active" stripped, "Users" survives, numeric value/measure untouched
    assert any("renamed" in w and "Active" in w for w in warnings)
    assert specs[0].measures[0].aggregation == "COUNT"  # the underlying query itself is unchanged


def test_plan_executive_report_kpis_respects_max_kpis(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.orders")
    _add_column(db, "dbo.orders", "OrderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    for i in range(4):
        _add_column(db, "dbo.orders", f"Metric{i}", data_type="DECIMAL", is_metric=True, business_label=f"Metric {i}")

    fake_ai_output = [
        {
            "title": f"Total Metric {i}", "business_question": f"What is total metric {i}?",
            "entity_name": "orders", "measures": [{"term": f"metric {i}", "aggregation": "SUM"}],
            "visualization_hint": "kpi_card", "importance": "medium", "reason": "x",
        }
        for i in range(10)
    ]
    with patch("core.reporting.report_planner._ai_propose_kpis", return_value=fake_ai_output):
        specs, _warnings = plan_executive_report_kpis(1, "u1", max_kpis=3)

    assert len(specs) == 3


# ---------------------------------------------------------------------------
# FINAL CORRECTNESS CLEANUP MILESTONE, Parts 1-3 — post-execution title
# semantic-consistency validator (find_unsupported_title_modifiers/
# repair_unsupported_title). Live-confirmed real-the reference database root-cause case:
# "Total Payments Due" executed a bare SUM(Amount) with no due/status
# filter/column anywhere in the plan.
# ---------------------------------------------------------------------------

def test_title_due_word_unsupported_without_matching_filter_or_column():
    sql_plan = {"select": [{"column_name": "Amount"}], "where": [], "group_by": []}
    assert find_unsupported_title_modifiers("Total Payments Due", sql_plan) == ["due"]


def test_title_due_word_supported_when_matching_filter_present():
    sql_plan = {
        "select": [{"column_name": "Amount"}],
        "where": [{"column_name": "DueDate", "operator": "IS NOT NULL", "value": None}],
        "group_by": [],
    }
    assert find_unsupported_title_modifiers("Total Payments Due", sql_plan) == []


def test_title_with_no_material_modifier_is_untouched():
    sql_plan = {"select": [{"column_name": "CompID"}], "where": [], "group_by": []}
    assert find_unsupported_title_modifiers("Total Clients", sql_plan) == []


def test_title_new_word_supported_by_insert_date_grouping():
    # "new" is satisfied by a creation-style date dimension (temporal
    # semantics), not just a literal "new" column name.
    sql_plan = {
        "select": [{"column_name": "CompID"}, {"column_name": "InsertDate"}],
        "where": [], "group_by": [{"column_name": "InsertDate"}],
    }
    assert find_unsupported_title_modifiers("Monthly New Clients", sql_plan) == []


def test_title_status_word_unsupported_without_status_column():
    sql_plan = {
        "select": [{"column_name": "ACHTransactionID"}, {"column_name": "TransactionDate"}],
        "where": [], "group_by": [{"column_name": "TransactionDate"}],
    }
    assert find_unsupported_title_modifiers("Monthly Payment Status Updates", sql_plan) == ["status"]


def test_title_active_word_supported_by_real_status_filter():
    sql_plan = {
        "select": [{"column_name": "ClassID"}],
        "where": [{"column_name": "IsActive", "operator": "=", "value": 1}],
        "group_by": [],
    }
    assert find_unsupported_title_modifiers("Active Classes", sql_plan) == []


def test_repair_strips_unsupported_word_and_cleans_whitespace():
    assert repair_unsupported_title("Total Payments Due", ["due"], "Total ACHTransaction") == "Total Payments"


def test_repair_falls_back_when_stripping_leaves_nothing():
    assert repair_unsupported_title("Due", ["due"], "Total Payments") == "Total Payments"
