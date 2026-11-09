"""
Executive Report Query Foundation milestone, Part 13 — proves the report
query foundation is NOT database-specific: it discovers KPIs from whatever
is_metric/is_dimension/is_date evidence a source actually has, and plans/
executes them via the same generic attempt_structured_kpi_plan used
throughout this milestone, regardless of domain vocabulary.

Exercises two different hypothetical schemas (logistics, healthcare-like
operational) — neither is the reference database's student/class/program/course-fee
vocabulary — through the real (non-mocked) resolution/validation path,
with only the LLM boundary (adjudicate_candidates) mocked, same convention
as tests/test_ai_structured_kpi_plan.py.

Run:
    python -m pytest tests/test_report_generic_schema.py -v
"""
from __future__ import annotations

from unittest.mock import patch

from core.orchestrator.ai_physical_planning import attempt_structured_kpi_plan
from core.reporting.report_planner import _deterministic_fallback_kpis, _is_low_relevance_infrastructure_table
from tests.ai_physical_planner_fixtures import _mock_adjudication
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_fk


def test_logistics_schema_shipment_delay_kpi(tmp_path, monkeypatch):
    # shipments/drivers/routes/delays — no vocabulary overlap with the reference database.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.shipments")
    _add_column(db, "dbo.shipments", "ShipmentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.shipments", "DelayMinutes", data_type="DECIMAL")
    _add_column(db, "dbo.shipments", "ShipDate", data_type="DATETIME", is_date=True)

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.shipments"),
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="shipment", business_question="Average delay by month",
            measures=[{"term": "delay", "aggregation": "AVG"}], calendar_grain="month",
        )

    assert result["valid"] is True, result["reason"]
    assert "AVG" in result["plans"][0]["generated"]["sql"]


def test_healthcare_operational_schema_appointment_count(tmp_path, monkeypatch):
    # appointments/providers/locations — a third, unrelated domain.
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.appointments")
    _add_column(db, "dbo.appointments", "AppointmentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    _add_table(db, "dbo.providers")
    _add_column(db, "dbo.providers", "ProviderID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.providers", "ProviderName", data_type="VARCHAR", is_dimension=True)
    _add_column(db, "dbo.appointments", "ProviderID", data_type="INTEGER")
    _add_fk(db, "dbo.appointments", "ProviderID", "dbo.providers", "ProviderID", status="AUTO")

    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        side_effect=[_mock_adjudication("dbo.appointments"), _mock_adjudication("dbo.providers")],
    ):
        result = attempt_structured_kpi_plan(
            1, "u1", entity_name="appointment", business_question="Appointment count by provider",
            measures=[{"term": "appointment", "aggregation": "COUNT"}], dimension_terms=["provider"],
        )

    assert result["valid"] is True, result["reason"]
    assert len(result["plans"][0]["sql_plan"]["group_by"]) == 1


def test_deterministic_fallback_adapts_to_ecommerce_evidence_shape():
    # Different domain vocabulary than every other fixture in this test
    # suite (customers/products/revenue, not students/programs) — the
    # fallback proposer must derive titles/questions purely from what the
    # evidence names, never a hardcoded the reference database template.
    evidence = [{
        "table_fqn": "dbo.order_items", "business_name": "Order Items", "domain": "E-Commerce", "entity": None,
        "metric_columns": [{"column_name": "LineTotal", "business_label": "Line Total"}],
        "dimension_columns": [{"column_name": "ProductCategory", "business_label": "Product Category"}],
        "date_columns": [{"column_name": "PurchasedAt", "business_label": "Purchased At"}],
    }]
    kpis = _deterministic_fallback_kpis(evidence)
    titles = " ".join(k["title"] for k in kpis)
    assert "Order Items" in titles
    assert "Line Total" in titles
    assert "Product Category" in " ".join(str(k.get("dimension_terms")) for k in kpis)
    assert not any("student" in t.lower() or "program" in t.lower() for t in titles.split())


# ---------------------------------------------------------------------------
# FINAL EXECUTIVE-REPORT BLOCKER FIX milestone, Part 8/9/11 —
# _is_low_relevance_infrastructure_table. Live-confirmed against a real production database:
# dbo.EDU_UserLogon / dbo.EDU_UserLegacyLogin (thin: no metric columns, no
# dimension columns, a bare Logon/date column) surfaced as executive KPI
# candidates ("Count of Users Logged In") despite being pure login-event
# stubs. These tests prove both halves: the thin+auth-lexical combination IS
# flagged, and — Part 11's explicit overfilter-protection requirement — a
# legitimate business-user table in ANY of the three named counter-example
# domains (SaaS active users, cybersecurity login events, HR employees) is
# NOT flagged, because it fails at least one of the two required conditions
# (real dimension columns, or real metric columns, or no auth-lexical match).
# ---------------------------------------------------------------------------

def _evidence_table(fqn, *, entity=None, metrics=None, dimensions=None, dates=None):
    return {
        "table_fqn": fqn, "business_name": fqn.split(".")[-1], "domain": "Operations", "entity": entity,
        "metric_columns": metrics or [], "dimension_columns": dimensions or [], "date_columns": dates or [],
    }


def test_thin_login_stub_table_is_flagged_infrastructure():
    table = _evidence_table(
        "dbo.EDU_UserLogon", entity="User",
        dates=[{"column_name": "LogonDate", "business_label": "Logon Date"}],
    )
    assert _is_low_relevance_infrastructure_table(table) is True


def test_thin_legacy_login_table_is_flagged_infrastructure():
    table = _evidence_table(
        "dbo.EDU_UserLegacyLogin", entity="User",
        dates=[{"column_name": "LogonDate", "business_label": "Logon Date"}],
    )
    assert _is_low_relevance_infrastructure_table(table) is True


def test_thin_non_auth_table_is_not_flagged():
    # Thin (no metrics/dimensions) but no auth/session vocabulary at all —
    # e.g. a genuine narrow business table — must survive.
    table = _evidence_table(
        "dbo.EDU_Student", entity="Student",
        dates=[{"column_name": "StartDate", "business_label": "Start Date"}],
    )
    assert _is_low_relevance_infrastructure_table(table) is False


def test_saas_active_users_table_is_not_flagged():
    # Part 11: "SaaS database: active users may be a real executive KPI."
    # Real dimension columns (plan tier, signup source, status) mean this is
    # a described business entity, not a bare login stub.
    table = _evidence_table(
        "dbo.Users", entity="User",
        dimensions=[
            {"column_name": "PlanTier", "business_label": "Plan Tier"},
            {"column_name": "SignupSource", "business_label": "Signup Source"},
            {"column_name": "AccountStatus", "business_label": "Account Status"},
        ],
    )
    assert _is_low_relevance_infrastructure_table(table) is False


def test_cybersecurity_login_events_table_is_not_flagged():
    # Part 11: "Cybersecurity database: login events may be core business
    # data." A security product's own core "Login Events" table carries
    # real outcome/method/geo dimension columns and/or metrics describing
    # the event, unlike a bare stub — the word "login" alone must not sink
    # it.
    table = _evidence_table(
        "dbo.LoginEvents", entity="Event",
        metrics=[{"column_name": "RiskScore", "business_label": "Risk Score"}],
        dimensions=[
            {"column_name": "AuthMethod", "business_label": "Auth Method"},
            {"column_name": "SourceCountry", "business_label": "Source Country"},
            {"column_name": "Outcome", "business_label": "Outcome"},
        ],
    )
    assert _is_low_relevance_infrastructure_table(table) is False


def test_hr_employees_table_is_not_flagged():
    # Part 11: "HR database: employees/users may be central entities."
    table = _evidence_table(
        "dbo.Employees", entity="Employee",
        dimensions=[
            {"column_name": "Department", "business_label": "Department"},
            {"column_name": "Title", "business_label": "Title"},
        ],
    )
    assert _is_low_relevance_infrastructure_table(table) is False


def test_table_with_metric_columns_is_never_flagged_even_if_auth_named():
    # A table with real quantifiable measures of its own is, by definition,
    # not a bare telemetry stub — condition (a) alone rules it out
    # regardless of how many dimension columns it has or what its name says.
    table = _evidence_table(
        "dbo.SessionBilling", entity="Session",
        metrics=[{"column_name": "BilledAmount", "business_label": "Billed Amount"}],
    )
    assert _is_low_relevance_infrastructure_table(table) is False
