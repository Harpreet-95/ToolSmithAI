"""
Day 2B, Task 3 — planner short-circuit integration.

Verifies data.query_planning_service._plan_business_query_impl (via the
public plan_business_query) correctly:
  - skips the broad _get_ai_candidate_tables_ranked search entirely when
    every matched entity is fully grounded (Task 6 performance goal),
  - additively unions grounded seeds into the broad-search result on
    PARTIAL grounding, never removing anything the search itself found,
  - is completely unaffected when no entity matches at all.

apply_grounding itself (matching + contract cache/build logic) is already
covered by tests/test_semantic_contract_service.py — these tests only
exercise the wiring: which branch runs, and whether _get_ai_candidate_
tables_ranked gets called.

Run:
    python -m pytest tests/test_semantic_contract_planner_integration.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2b-planner-integration-secret-long-enou1")
os.environ.setdefault("USER_ID_SALT", "test-day2b-planner-integration-salt-long-enough2")

import data.semantic_contract_service as scs
import data.query_planning_service as qps
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain


def _plan(question, concepts, measures, dimensions=None):
    return qps.plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions or [],
    })


def test_full_grounding_skips_broad_retrieval(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dnnuser.FIN_Invoice", table_class="Transactional")
    _add_column(db, "dnnuser.FIN_Invoice", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dnnuser.FIN_Invoice", "Operations")

    fake_grounding = {
        "matched_entities": ["Invoice"],
        "grounded": {"Invoice": {"canonical_table_fqn": "dnnuser.FIN_Invoice"}},
        "fully_grounded": True,
        "seed_table_fqns": {"dnnuser.FIN_Invoice"},
        "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    result = _plan("Active invoices", ["invoices"], ["invoices"])
    assert calls == []  # broad retrieval never called
    assert result["tables"] == ["dnnuser.FIN_Invoice"]


def test_partial_grounding_unions_seed_without_removing_broad_results(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Recruiters", table_class="Transactional")
    _add_column(db, "dbo.Recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Recruiters", "Operations")
    _add_table(db, "dbo.SomeOtherTable", table_class="Transactional")
    _add_column(db, "dbo.SomeOtherTable", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    fake_grounding = {
        "matched_entities": ["Recruiter", "Placement"],
        "grounded": {"Recruiter": {"canonical_table_fqn": "dbo.Recruiters"}},
        "fully_grounded": False,
        "seed_table_fqns": {"dbo.Recruiters"},
        "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        candidate_tables, ranking, remembered = real_retrieval(*a, **k)
        # Simulate the broad search independently finding a DIFFERENT table
        # (SomeOtherTable) so the union behavior is actually observable.
        return ({"dbo.SomeOtherTable"} | candidate_tables), ranking, remembered

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    result = _plan(
        "Show recruiters and their placements", ["recruiters", "placements"], ["recruiters", "placements"],
    )
    assert calls == [1]  # broad retrieval WAS called (partial grounding, not a full skip)
    assert "dbo.Recruiters" in result["tables"]  # contract seed present
    assert "dbo.SomeOtherTable" in result["tables"]  # broad-search result preserved, not removed


def test_grounded_table_wins_role_selection_over_higher_scoring_broad_search_table(tmp_path, monkeypatch):
    """Day 2C, Task 4 — the gap the original partial-grounding test above
    never covered: under PARTIAL multi-entity grounding, a broad-search
    table that scores EQUAL OR HIGHER than the grounded contract's own
    canonical table for the SAME entity role must still lose. dbo.
    BetterRecruiterMatch is deliberately given stronger generic authority
    evidence (domain assignment, approved dictionary business_name) than
    dbo.Recruiters (the grounded contract's own canonical table, no domain/
    dictionary at all) — without Task 4's per-term narrowing, generic
    _score_table_authority scoring would let the broad-search table win.
    """
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Recruiters", table_class="Transactional")
    _add_column(db, "dbo.Recruiters", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    # Deliberately NO domain/dictionary evidence — the grounded contract's
    # canonical table must win purely because it IS the grounded contract,
    # not because it also happens to out-score everything generically.

    _add_table(db, "dbo.BetterRecruiterMatch", table_class="Transactional")
    _add_column(db, "dbo.BetterRecruiterMatch", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.BetterRecruiterMatch", "Operations")

    fake_grounding = {
        "matched_entities": ["Recruiter", "Placement"],
        "grounded": {"Recruiter": {"canonical_table_fqn": "dbo.Recruiters"}},
        "fully_grounded": False,
        "seed_table_fqns": {"dbo.Recruiters"},
        "entity_table_map": {"Recruiter": "dbo.Recruiters"},
        "entity_trusted_tables": {"Recruiter": {"dbo.Recruiters"}},
        "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        candidate_tables, ranking, remembered = real_retrieval(*a, **k)
        # Broad search independently finds the higher-scoring competitor —
        # both tables end up in the same candidate pool, exactly the
        # partial-grounding shape found on a real production database.
        return ({"dbo.BetterRecruiterMatch"} | candidate_tables), ranking, remembered

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    result = _plan(
        "Show recruiters and their placements", ["recruiters", "placements"], ["recruiters", "placements"],
    )
    recruiter_concept = next(c for c in result["concepts"] if c["term"] == "recruiters")
    assert recruiter_concept["selected"] is not None
    assert recruiter_concept["selected"]["table_fqn"] == "dbo.Recruiters"


def test_no_entity_match_leaves_broad_retrieval_untouched(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Widgets", table_class="Transactional")
    _add_column(db, "dbo.Widgets", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    fake_grounding = {
        "matched_entities": [], "grounded": {}, "fully_grounded": False,
        "seed_table_fqns": set(), "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    _plan("Show widgets", ["widgets"], ["widgets"])
    assert calls == [1]


def test_status_hint_from_contract_used_over_generic_keyword_search(tmp_path, monkeypatch):
    """Task 4 — when a grounded contract already verified a status column
    on the resolved table, that column (and the matched verified value's
    own casing) is used directly instead of _find_status_column's generic
    keyword scan."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.JobOrders", table_class="Transactional")
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.JobOrders", "job_status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Job Status", approved=True)
    _add_domain(db, "dbo.JobOrders", "Operations")

    fake_grounding = {
        "matched_entities": ["Job Order"],
        "grounded": {"Job Order": {"canonical_table_fqn": "dbo.JobOrders"}},
        "fully_grounded": True,
        "seed_table_fqns": {"dbo.JobOrders"},
        "status_hints": {
            "dbo.JobOrders": {
                "column_name": "job_status", "data_type": "TEXT", "verified_values": ["open", "closed"],
            },
        },
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    result = _plan("List open job orders", ["job", "orders"], ["job", "orders"])
    status_filters = [f for f in result["filters"] if f.get("column") == "job_status"]
    assert len(status_filters) == 1
    assert status_filters[0]["value"] == "open"  # contract's own verified casing, not the raw "Open" question word
    assert status_filters[0]["table_fqn"] == "dbo.JobOrders"  # completely normal path


# ---------------------------------------------------------------------------
# LIVE the reference database VERIFICATION MILESTONE, Part 1 — regression test for a bug that
# ONLY manifested via this contract-status-hint (grounding) path, and was
# missed by every earlier unit test because those exercised the OTHER
# branch (_find_status_column / _typed_status_filter_value, no grounding).
# Live-confirmed against a real production database: "List classes that are not active"
# produced "IsActive != 0" (double-negated: the value was already flipped
# 1->0, then the operator was ALSO independently flipped to "!="), which
# SQL Server satisfies only for IsActive = 1 — the ACTIVE rows, the exact
# opposite of what was asked. The fix: the BOOLEAN branch decides its own
# operator ("=", with the value flipped) instead of falling through to a
# shared "!= if negated" default meant for the TEXT branch.
# ---------------------------------------------------------------------------

def test_negated_boolean_status_hint_does_not_double_negate(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Class", table_class="Transactional")
    _add_column(db, "dbo.EDU_Class", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Class", "IsActive", data_type="BOOLEAN", is_dimension=True,
                cardinality_tier="BINARY", business_label="Is Active")
    _add_domain(db, "dbo.EDU_Class", "Operations")

    fake_grounding = {
        "matched_entities": ["Class"],
        "grounded": {"Class": {"canonical_table_fqn": "dbo.EDU_Class"}},
        "fully_grounded": True,
        "seed_table_fqns": {"dbo.EDU_Class"},
        "status_hints": {
            "dbo.EDU_Class": {"column_name": "IsActive", "data_type": "BOOLEAN", "verified_values": []},
        },
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    result = _plan(
        "List classes that are not active", ["classes"], ["classes"],
    )
    status_filters = [f for f in result["filters"] if f.get("column") == "IsActive"]
    assert len(status_filters) == 1
    # Must be a single, correct negation: "=", 0 -- never "!=", 0 (which SQL
    # evaluates true only for the ACTIVE rows, the opposite polarity).
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["value"] == 0
    assert status_filters[0]["polarity"] == "negative"


def test_positive_boolean_status_hint_unaffected(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Class", table_class="Transactional")
    _add_column(db, "dbo.EDU_Class", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.EDU_Class", "IsActive", data_type="BOOLEAN", is_dimension=True,
                cardinality_tier="BINARY", business_label="Is Active")
    _add_domain(db, "dbo.EDU_Class", "Operations")

    fake_grounding = {
        "matched_entities": ["Class"],
        "grounded": {"Class": {"canonical_table_fqn": "dbo.EDU_Class"}},
        "fully_grounded": True,
        "seed_table_fqns": {"dbo.EDU_Class"},
        "status_hints": {
            "dbo.EDU_Class": {"column_name": "IsActive", "data_type": "BOOLEAN", "verified_values": []},
        },
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    result = _plan("List active classes", ["classes"], ["classes"])
    status_filters = [f for f in result["filters"] if f.get("column") == "IsActive"]
    assert len(status_filters) == 1
    assert status_filters[0]["operator"] == "="
    assert status_filters[0]["value"] == 1
    assert status_filters[0]["polarity"] == "positive"


def test_negated_text_status_hint_flips_operator_not_value(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.JobOrders", table_class="Transactional")
    _add_column(db, "dbo.JobOrders", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.JobOrders", "job_status", data_type="TEXT", is_dimension=True,
                cardinality_tier="LOW", business_label="Job Status", approved=True)
    _add_domain(db, "dbo.JobOrders", "Operations")

    fake_grounding = {
        "matched_entities": ["Job Order"],
        "grounded": {"Job Order": {"canonical_table_fqn": "dbo.JobOrders"}},
        "fully_grounded": True,
        "seed_table_fqns": {"dbo.JobOrders"},
        "status_hints": {
            "dbo.JobOrders": {
                "column_name": "job_status", "data_type": "TEXT", "verified_values": ["open", "closed"],
            },
        },
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    result = _plan("List job orders that are not open", ["job", "orders"], ["job", "orders"])
    status_filters = [f for f in result["filters"] if f.get("column") == "job_status"]
    assert len(status_filters) == 1
    assert status_filters[0]["operator"] == "!="
    assert status_filters[0]["value"] == "open"  # value unchanged, only operator flips for TEXT
    assert status_filters[0]["polarity"] == "negative"
