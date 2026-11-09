"""
Day 2A, Task 3 — Entity-First List Routing.

Confirmed live defect (root-cause trace against a real production database metadata): "List
open job orders" correctly resolves its concept to dbo.EDU_StaffingJobs, but the
same bare term also incidentally name-matches an unrelated pre-aggregated
rollup column (dbo.EDU_StaffingClientContacts.TotalJobOrders). That incidental
measure match unconditionally forced intent.type="aggregate_overall" with
aggregation="SUM", silently overriding an explicit list request that used
no aggregation language at all.

Unit-tests data.query_planning_service._build_intent directly (a pure
function of question/measures/dimensions/query_intent/resolved_concepts —
no DB) so these tests exercise exactly the routing decision Task 3 targets.
SYNTHETIC terms/tables in the generic test (never a the reference database object name); the
job-orders/invoice-specific tests mirror the confirmed real defect and are
plain English business language, not physical identifiers.

Run:
    python -m pytest tests/test_entity_first_list_routing.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2a-task3-entity-first-routing-secret-lo1")
os.environ.setdefault("USER_ID_SALT", "test-day2a-task3-entity-first-routing-salt-lon12")

from data.query_planning_service import _build_intent

_NO_AGG_INTENT = {"aggregation": None, "aggregation_target": None, "distinct": False, "order": None}


def _concept(term, table_fqn):
    return {"term": term, "resolved": True, "selected": {"table_fqn": table_fqn, "score": 0.9},
            "candidates": [], "confidence": 0.9, "ambiguity_reason": None}


def _measure(term, table_fqn, column_name, *, score=0.8, business_label=None):
    return {
        "term": term,
        "selected": {
            "table_fqn": table_fqn, "column_name": column_name, "score": score,
            "business_label": business_label or column_name,
        },
        "candidates": [], "warnings": [],
    }


def _unselected_measure(term):
    return {"term": term, "selected": None, "candidates": [], "warnings": []}


def _dimension(term, table_fqn, column_name):
    return {"term": term, "selected": {"table_fqn": table_fqn, "column_name": column_name, "score": 0.8},
            "candidates": [], "warnings": [], "semantic_compatibility": None}


# ---------------------------------------------------------------------------
# 1 — List open job orders -> list_entities, no SUM
# ---------------------------------------------------------------------------

def test_list_open_job_orders_routes_to_list_entities_not_sum():
    concepts = [_concept("job order", "dbo.EDU_StaffingJobs")]
    measures = [_measure("job order", "dbo.EDU_StaffingClientContacts", "TotalJobOrders", business_label="Total Job Orders")]
    intent = _build_intent("List open job orders", measures, [], dict(_NO_AGG_INTENT), concepts)
    assert intent["type"] == "list_entities"
    assert intent["aggregation"] is None


# ---------------------------------------------------------------------------
# 2 — Show active invoices -> list_entities
# ---------------------------------------------------------------------------

def test_show_active_invoices_routes_to_list_entities():
    concepts = [_concept("invoice", "dbo.invoices")]
    measures = [_unselected_measure("invoice")]
    intent = _build_intent("Show active invoices", measures, [], dict(_NO_AGG_INTENT), concepts)
    assert intent["type"] == "list_entities"
    assert intent["aggregation"] is None


# ---------------------------------------------------------------------------
# 3 — Total job orders by client -> aggregate/grouped (explicit "total")
# ---------------------------------------------------------------------------

def test_total_job_orders_by_client_stays_aggregate_by_dimension():
    query_intent = {**_NO_AGG_INTENT, "aggregation": "SUM"}
    concepts = [_concept("job order", "dbo.EDU_StaffingJobs")]
    measures = [_measure("job order", "dbo.EDU_StaffingClientContacts", "TotalJobOrders")]
    dimensions = [_dimension("client", "dbo.clients", "name")]
    intent = _build_intent("Total job orders by client", measures, dimensions, query_intent, concepts)
    assert intent["type"] == "aggregate_by_dimension"
    assert intent["aggregation"] == "SUM"


# ---------------------------------------------------------------------------
# 4 — Average invoice amount -> aggregate (explicit "average")
# ---------------------------------------------------------------------------

def test_average_invoice_amount_stays_aggregate():
    query_intent = {**_NO_AGG_INTENT, "aggregation": "AVG"}
    concepts = [_concept("invoice", "dbo.invoices")]
    measures = [_measure("amount", "dbo.invoices", "amount")]
    intent = _build_intent("Average invoice amount", measures, [], query_intent, concepts)
    assert intent["type"] == "aggregate_overall"
    assert intent["aggregation"] == "AVG"


# ---------------------------------------------------------------------------
# 5 — How many open job orders -> count (explicit "how many")
# ---------------------------------------------------------------------------

def test_how_many_open_job_orders_stays_count():
    query_intent = {**_NO_AGG_INTENT, "aggregation": "COUNT", "aggregation_target": "entity_count"}
    concepts = [_concept("job order", "dbo.EDU_StaffingJobs")]
    # Entity-count resolution (_resolve_entity_count) selects COUNT(*) on the
    # entity's own table with column_name=None — same shape as the real path.
    measures = [_measure("job order", "dbo.EDU_StaffingJobs", None)]
    intent = _build_intent("How many open job orders", measures, [], query_intent, concepts)
    assert intent["type"] == "aggregate_overall"
    assert intent["aggregation"] == "COUNT"


# ---------------------------------------------------------------------------
# 6 — an unrelated rollup measure cannot hijack list routing (generic,
# synthetic — proves the mechanism, not a job-orders special case)
# ---------------------------------------------------------------------------

def test_unrelated_rollup_measure_cannot_hijack_list_routing():
    concepts = [_concept("report", "dbo.Reports")]
    measures = [_measure("report", "dbo.MonthlySummary", "TotalReports")]
    intent = _build_intent("List reports", measures, [], dict(_NO_AGG_INTENT), concepts)
    assert intent["type"] == "list_entities"
    assert intent["aggregation"] is None


# ---------------------------------------------------------------------------
# Same-table measure still overrides list routing even without aggregation
# language, when it genuinely belongs to the resolved entity (Task 3, rule b)
# ---------------------------------------------------------------------------

def test_same_table_measure_overrides_without_aggregation_language():
    concepts = [_concept("client", "dbo.clients")]
    measures = [_measure("revenue", "dbo.clients", "revenue")]
    intent = _build_intent("Show client revenue", measures, [], dict(_NO_AGG_INTENT), concepts)
    assert intent["type"] == "aggregate_overall"
