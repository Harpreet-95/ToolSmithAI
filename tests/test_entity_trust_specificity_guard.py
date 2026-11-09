"""
Day 2A, Task 2 — Trust- and Specificity-Aware Entity Selection.

Confirmed live defect (root-cause trace against a real production database metadata): for
"Active invoices", dbo.orders (dictionary_approved=False, domain=Unknown)
outranked dbo.EDU_Payment_Invoices (domain_assigned=True, a genuinely
specific invoices business object) purely because "invoices" expands to the
literal synonym "order" (data/vocabulary_service.expand_concept's governed
synonym group), giving dbo.orders a perfect name_score=1.0 via an exact
string match on its own short, generic table name — while the real, more
specific invoices table only partially token-matches and scores 0.479.

Unit-tests data.query_planning_service._resolve_concept directly (a pure
function of a hand-built table_contexts dict — no DB, no retrieval-layer
search index needed) so these tests exercise exactly the ranking/selection
logic Task 2 targets. SYNTHETIC terms/table names throughout (never
"invoice"/"orders"/any the reference database object name) — the mechanism under test is
general: a short, generic, UNGOVERNED table name winning purely via synonym
expansion against a longer, GOVERNED, more specific table name.

Run:
    python -m pytest tests/test_entity_trust_specificity_guard.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2a-task2-trust-specificity-secret-long-1")
os.environ.setdefault("USER_ID_SALT", "test-day2a-task2-trust-specificity-salt-long-12")

import data.query_planning_service as qps


def _ctx(
    *, business_name=None, description=None, is_approved=False,
    domain=None, entity=None, table_type="TABLE",
):
    """Minimal table_contexts[table_fqn] entry — only the fields
    _resolve_concept/_score_table_authority actually read, matching
    data.business_knowledge_service.get_table_business_context's real shape
    field-for-field (dictionary/domain/entity/governance/profiling/
    relationships/relationship_evidence/table)."""
    domain = domain or "Unknown"
    entity = entity or "Unknown"
    return {
        "dictionary": {
            "business_name": business_name, "description": description,
            "is_approved": is_approved,
        },
        "domain": {"domain": domain, "confidence": 0.88},
        "entity": {"entity": entity, "confidence": 0.8},
        "governance": {
            "dictionary_approved": is_approved,
            "domain_assigned": domain != "Unknown",
            "entity_assigned": entity != "Unknown",
        },
        "profiling": {
            "is_root_table": False, "table_class": "Transactional",
            "exact_row_count": 100, "estimated_row_count": None,
            "is_junction_table": False,
        },
        "relationships": {"outbound": [], "inbound": []},
        "relationship_evidence": [],
        "table": {"table_type": table_type},
    }


def _fake_expand(mapping):
    def _expand(term):
        return [term, *mapping.get(term, [])]
    return _expand


# ---------------------------------------------------------------------------
# Test 1 — a generic, unapproved, unknown-domain candidate must not beat a
# more specific, domain-assigned candidate purely on synonym-expanded
# name_score.
# ---------------------------------------------------------------------------

def test_generic_unapproved_candidate_cannot_beat_specific_governed_candidate(monkeypatch):
    monkeypatch.setattr(qps, "expand_concept", _fake_expand({"widgets": ["gadgets"]}))
    table_contexts = {
        # Generic, ungoverned table — wins on raw name_score via synonym
        # expansion alone (its own name/business_name is "gadgets", an exact
        # match for the expanded synonym term).
        "dbo.gadgets": _ctx(business_name="Gadgets", description="Contains gadget data."),
        # Specific, governed table — the real business object for "widgets"
        # (mirrors the real "Active invoices" case, where the correct table
        # was dictionary-approved and domain-assigned, just not a perfect
        # literal name match).
        "dbo.SpecialtyWidgets": _ctx(
            business_name="Specialty Widgets", description="Contains widget data.",
            domain="Operations", entity="Widget", is_approved=True,
        ),
    }

    result = qps._resolve_concept("widgets", table_contexts)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.SpecialtyWidgets"


# ---------------------------------------------------------------------------
# Test 2 — trusted curated vocabulary may still win when nothing governed
# competes with it (the guard is comparative, not a blanket penalty).
# ---------------------------------------------------------------------------

def test_curated_vocabulary_wins_without_a_governed_competitor(monkeypatch):
    monkeypatch.setattr(qps, "expand_concept", _fake_expand({"widgets": ["gadgets"]}))
    table_contexts = {
        "dbo.gadgets": _ctx(business_name="Gadgets", description="Contains gadget data."),
    }

    result = qps._resolve_concept("widgets", table_contexts)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.gadgets"


# ---------------------------------------------------------------------------
# Test 3 — two genuinely separate GOVERNED systems remain ambiguous; the
# guard only discounts an ungoverned top candidate, never arbitrates
# between two governed ones.
# ---------------------------------------------------------------------------

def test_two_governed_systems_remain_ambiguous(monkeypatch):
    monkeypatch.setattr(qps, "expand_concept", _fake_expand({}))
    table_contexts = {
        "dbo.WidgetsPrimary": _ctx(
            business_name="Widgets Primary", description="Contains widget data.",
            is_approved=True, domain="Operations",
        ),
        "dbo.WidgetsSecondary": _ctx(
            business_name="Widgets Secondary", description="Contains widget data.",
            is_approved=True, domain="Operations",
        ),
    }

    result = qps._resolve_concept("widgets", table_contexts)
    assert result["selected"] is None
    assert result["ambiguity_reason"] is not None


# ---------------------------------------------------------------------------
# Test 4 — currently passing entity selections remain unchanged: a plain,
# unambiguous, non-synonym-assisted single-candidate match is untouched.
# ---------------------------------------------------------------------------

def test_unambiguous_direct_match_unaffected_by_guard(monkeypatch):
    monkeypatch.setattr(qps, "expand_concept", _fake_expand({}))
    table_contexts = {
        "dbo.clients": _ctx(business_name="Clients", description="Contains client data.", is_approved=True),
    }

    result = qps._resolve_concept("clients", table_contexts)
    assert result["selected"] is not None
    assert result["selected"]["table_fqn"] == "dbo.clients"
    assert result["selected"]["score"] == 1.0


def test_full_backend_suite_regression_sample_unaffected(monkeypatch):
    """A governed candidate winning purely on its own genuine name/business
    match (no synonym expansion involved at all) must score identically to
    before the guard — proves the guard is a no-op outside its narrow
    trigger condition."""
    monkeypatch.setattr(qps, "expand_concept", _fake_expand({}))
    table_contexts = {
        "dbo.orders_view_stub": _ctx(
            business_name="Orders", description="Contains order data.",
            is_approved=True, domain="Operations",
        ),
    }
    result = qps._resolve_concept("orders", table_contexts)
    assert result["selected"] is not None
    assert result["selected"]["score"] == 1.0
