"""
Tests for Milestone Phase 6.1 — Semantic Correctness Guard.

Two independent safeguards, both added to the existing SQL Planning &
Validation Engine without introducing a new planner/ranking engine:

  1. Semantic-family compatibility (core/semantic/compatibility_guard.py,
     wired into data/query_planning_service.py::_resolve_term and enforced
     as a hard block in data/sql_planning_service.py::build_sql_plan).
  2. Plan-integrity table-membership guard (build_sql_plan) — every
     selected measure/dimension/filter must reference a table that is
     actually in the FROM/JOIN graph.

Follows the same hand-built query_plan fixture pattern as
tests/test_phase10_sql_planning.py rather than re-running full discovery,
so these tests exercise build_sql_plan's own validation logic in isolation.

Run from the project root:
    venv/Scripts/pytest tests/test_semantic_compatibility_guard.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-phase6-1-semantic-guard-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-phase6-1-salt-long-enough-value-123456789")

from core.semantic.compatibility_guard import check_compatibility, infer_term_family
from data.sql_generation_service import generate_sql
from data.sql_planning_service import build_sql_plan


# ---------------------------------------------------------------------------
# Fixtures — mirrors test_phase10_sql_planning.py's shape exactly
# ---------------------------------------------------------------------------

def _measure(term, table_fqn, column_name, selected=True, semantic_compatibility=None):
    sel = {"table_fqn": table_fqn, "column_name": column_name, "business_label": None,
           "score": 0.9, "is_approved": True, "data_type": "DECIMAL"} if selected else None
    return {"term": term, "selected": sel, "candidates": [sel] if sel else [],
            "warnings": [], "semantic_compatibility": semantic_compatibility}


def _dimension(term, table_fqn, column_name, selected=True, semantic_compatibility=None):
    sel = {"table_fqn": table_fqn, "column_name": column_name, "business_label": None,
           "score": 0.9, "is_approved": True, "data_type": "TEXT"} if selected else None
    return {"term": term, "selected": sel, "candidates": [sel] if sel else [],
            "warnings": [], "semantic_compatibility": semantic_compatibility}


def _join_step(from_table, from_col, to_table, to_col, *, path_found=True,
                join_type="INNER", cardinality="MANY_TO_ONE", fanout_risk="LOW", confidence=100):
    return {
        "from_table": from_table, "from_column": from_col,
        "to_table": to_table, "to_column": to_col,
        "path_found": path_found, "hops": 1 if path_found else None,
        "join_type": join_type if path_found else None,
        "cardinality": cardinality if path_found else None,
        "fanout_risk": fanout_risk if path_found else None,
        "fanout_explanation": "structural", "join_quality": 80 if path_found else None,
        "join_quality_tier": "HIGH" if path_found else None,
        "relationship_strength": "STRONG" if path_found else None,
        "confidence": confidence,
    }


def _base_plan(*, measures=None, dimensions=None, columns=None, join_plan=None,
                filters=None, aggregation="SUM"):
    return {
        "intent": {"raw_question": "test", "type": "aggregate_by_dimension", "aggregation": aggregation},
        "tables": list((columns or {}).keys()),
        "columns": columns or {},
        "measures": measures or [],
        "dimensions": dimensions or [],
        "filters": filters or [],
        "join_plan": join_plan or {
            "required": False, "tables": [], "primary_table": None,
            "steps": [], "fanout_risk": None, "confidence": 100,
        },
        "warnings": [],
        "confidence": 80,
        "explanation": "test plan",
    }


def _resolved_filter(table_fqn, column_name, value="Active", operator="="):
    return {"column": column_name, "field": column_name, "operator": operator, "value": value,
            "resolved": True, "table_fqn": table_fqn}


# ---------------------------------------------------------------------------
# A. core/semantic/compatibility_guard.py — pure unit tests, no DB
# ---------------------------------------------------------------------------

def test_infer_term_family_money_terms():
    # "payroll" itself is not in the reused AMOUNT vocabulary (amount, cost,
    # revenue, salary, wage, ...) — it's a domain concept, not a financial
    # noun by name, so it correctly carries no family signal on its own.
    for term in ("revenue", "salary", "cost", "invoice amount"):
        assert infer_term_family(term) == "AMOUNT", term
    assert infer_term_family("payroll") is None


def test_infer_term_family_temporal_terms():
    for term in ("year", "this year", "month", "quarter"):
        assert infer_term_family(term) == "DATE", term


def test_infer_term_family_contact_terms():
    assert infer_term_family("phone") == "PHONE"
    assert infer_term_family("email") == "EMAIL"


def test_infer_term_family_ordinary_noun_is_none():
    for term in ("clients", "students", "recruiters", "projects"):
        assert infer_term_family(term) is None, term


def test_compatibility_revenue_vs_years_experience():
    result = check_compatibility("year", "DATE", "AMOUNT")
    assert result.compatible is False
    assert "temporal" in result.reason
    assert "AMOUNT" in result.reason


def test_compatibility_salary_vs_phone():
    result = check_compatibility("salary", "AMOUNT", "PHONE")
    assert result.compatible is False


def test_compatibility_invoice_vs_hire_date_boundary():
    # "invoice" itself carries no strong family signal (not in the AMOUNT
    # vocabulary) — must not block on a guess.
    assert infer_term_family("invoice") is None
    result = check_compatibility("invoice", infer_term_family("invoice"), "DATE")
    assert result.compatible is True


def test_compatibility_student_count_vs_bank_account_shaped_column():
    # "count" implies QUANTITY; an ID-family column (bank-account-shaped
    # identifier) is a different group -> incompatible.
    result = check_compatibility("student count", infer_term_family("student count"), "ID")
    assert result.compatible is False


def test_compatibility_payroll_amount_vs_phone():
    term_family = infer_term_family("payroll amount")
    assert term_family == "AMOUNT"
    result = check_compatibility("payroll amount", term_family, "PHONE")
    assert result.compatible is False


def test_compatibility_invoice_total_vs_address_shaped_column():
    # "invoice total" carries AMOUNT via "total"; an address/name-shaped
    # column has no strong profiling family (NAME/TEXT) -> no opinion either
    # way, so this must not be misclassified as an incompatible family pair.
    term_family = infer_term_family("invoice total")
    assert term_family == "AMOUNT"
    result = check_compatibility("invoice total", term_family, "NAME")
    assert result.compatible is True  # NAME carries no family group -> no opinion


def test_compatibility_client_count_vs_employee_count_is_valid():
    # Same family (QUANTITY) on both sides -> a legitimate combination.
    result = check_compatibility("client count", infer_term_family("client count"), "COUNT")
    assert result.compatible is True


def test_compatibility_student_count_vs_payroll_amount_column():
    # "count" (QUANTITY) against a column profiling has classified AMOUNT
    # (money) -> different groups -> incompatible.
    result = check_compatibility("student count", infer_term_family("student count"), "AMOUNT")
    assert result.compatible is False


def test_compatibility_same_family_is_valid():
    result = check_compatibility("revenue", "AMOUNT", "AMOUNT")
    assert result.compatible is True


def test_compatibility_unknown_column_family_never_blocks():
    # Column has no confident profiling classification (UNKNOWN/None) —
    # silence, not a guess, so it must not block.
    assert check_compatibility("revenue", "AMOUNT", None).compatible is True
    assert check_compatibility("revenue", "AMOUNT", "UNKNOWN").compatible is True


# ---------------------------------------------------------------------------
# B. build_sql_plan — semantic-compatibility hard block
# ---------------------------------------------------------------------------

def test_semantic_incompatible_measure_blocks_plan():
    qp = _base_plan(
        measures=[_measure("year", "dbo.ADF_YearExpRq", "YearsExpValue", semantic_compatibility={
            "compatible": False, "requested_measure": "year",
            "resolved_concept": "dbo.ADF_YearExpRq.YearsExpValue",
            "term_family": "DATE", "column_family": "AMOUNT",
            "reason": "mismatch", "confidence": 0.94, "suggested": None,
        })],
        columns={"dbo.ADF_YearExpRq": ["YearsExpValue"]},
        join_plan={"required": False, "tables": ["dbo.ADF_YearExpRq"],
                   "primary_table": "dbo.ADF_YearExpRq", "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["validation"]["checks"]["semantic_compatible"] is False
    assert any("Semantic incompatibility" in r for r in plan["validation"]["blocking_reasons"])

    sql_gen = generate_sql(1, "u1", plan)
    assert sql_gen["sql"] is None


def test_semantically_compatible_measure_is_unaffected():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],  # no semantic_compatibility key set
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert plan["validation"]["checks"]["semantic_compatible"] is True

    sql_gen = generate_sql(1, "u1", plan)
    assert sql_gen["sql"] is not None


# ---------------------------------------------------------------------------
# C. build_sql_plan — plan-integrity table-membership guard
# ---------------------------------------------------------------------------

def test_date_filter_outside_from_join_blocks_plan():
    qp = _base_plan(
        measures=[_measure("count", "dbo.jobs", None)],
        columns={"dbo.jobs": [], "dbo.unrelated_staff_table": ["HireDate"]},
        join_plan={"required": False, "tables": ["dbo.jobs"], "primary_table": "dbo.jobs",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        filters=[_resolved_filter("dbo.unrelated_staff_table", "HireDate", value=["2026-01-01", "2026-12-31"], operator="BETWEEN")],
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["validation"]["checks"]["all_references_in_query_graph"] is False
    assert any("outside the FROM/JOIN graph" in r for r in plan["validation"]["blocking_reasons"])
    assert any("dbo.unrelated_staff_table.HireDate" in r for r in plan["validation"]["blocking_reasons"])

    sql_gen = generate_sql(1, "u1", plan)
    assert sql_gen["sql"] is None


def test_status_filter_outside_from_join_blocks_plan():
    qp = _base_plan(
        measures=[_measure("count", "dbo.jobs", None)],
        columns={"dbo.jobs": [], "dbo.unrelated_staff_table": ["EmploymentStatus"]},
        join_plan={"required": False, "tables": ["dbo.jobs"], "primary_table": "dbo.jobs",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        filters=[_resolved_filter("dbo.unrelated_staff_table", "EmploymentStatus", value="Active")],
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is False
    assert plan["validation"]["checks"]["all_references_in_query_graph"] is False
    assert any("dbo.unrelated_staff_table.EmploymentStatus" in r for r in plan["validation"]["blocking_reasons"])


def test_filter_on_driving_table_is_valid():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount", "status"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        filters=[_resolved_filter("dbo.orders", "status", value="Active")],
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert plan["validation"]["checks"]["all_references_in_query_graph"] is True
    assert len(plan["where"]) == 1


def test_filter_on_approved_joined_table_is_valid():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        dimensions=[_dimension("customer", "dbo.customers", "name")],
        columns={"dbo.orders": ["amount", "customer_id"], "dbo.customers": ["name", "region"]},
        join_plan={
            "required": True, "tables": ["dbo.orders", "dbo.customers"], "primary_table": "dbo.orders",
            "steps": [_join_step("dbo.orders", "customer_id", "dbo.customers", "id")],
            "fanout_risk": "LOW", "confidence": 95,
        },
        filters=[_resolved_filter("dbo.customers", "region", value="West")],
    )
    plan = build_sql_plan(1, "u1", qp)
    assert plan["validation"]["valid"] is True
    assert plan["validation"]["checks"]["all_references_in_query_graph"] is True

    sql_gen = generate_sql(1, "u1", plan)
    assert sql_gen["sql"] is not None
    assert "customers" in sql_gen["sql"]


def test_out_of_graph_filter_causes_generate_sql_refusal():
    qp = _base_plan(
        measures=[_measure("revenue", "dbo.orders", "amount")],
        columns={"dbo.orders": ["amount"], "dbo.other": ["created_at"]},
        join_plan={"required": False, "tables": ["dbo.orders"], "primary_table": "dbo.orders",
                   "steps": [], "fanout_risk": None, "confidence": 100},
        filters=[_resolved_filter("dbo.other", "created_at", value=["2026-01-01", "2026-12-31"], operator="BETWEEN")],
    )
    plan = build_sql_plan(1, "u1", qp)
    sql_gen = generate_sql(1, "u1", plan)
    assert sql_gen["sql"] is None
    assert any("SQL plan validation failed" in e or "outside the FROM/JOIN graph" in e for e in sql_gen["explanation"])
