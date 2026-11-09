"""
Day 2C, Task 4 — Role-Selection Control.

Pure unit tests for data.query_planning_service._grounded_trusted_tables_for_
term / _narrow_to_grounded_table. End-to-end integration coverage (via
plan_business_query) lives in tests/test_semantic_contract_planner_integration
.py and tests/test_status_resolution_bounded_retry.py.

Run:
    python -m pytest tests/test_role_selection_control.py -v
"""
from data.query_planning_service import (
    _grounded_trusted_tables_for_term,
    _narrow_to_grounded_table,
)


def _grounding(entity_table_map=None, entity_trusted_tables=None):
    return {
        "entity_table_map": entity_table_map or {},
        "entity_trusted_tables": entity_trusted_tables or {},
    }


def test_returns_none_without_grounding():
    assert _grounded_trusted_tables_for_term("recruiters", None) is None


def test_returns_none_when_term_names_no_entity():
    grounding = _grounding({"Recruiter": "dbo.Recruiters"}, {"Recruiter": {"dbo.Recruiters"}})
    assert _grounded_trusted_tables_for_term("widgets", grounding) is None


def test_returns_trusted_set_when_term_names_a_grounded_entity():
    grounding = _grounding(
        {"Recruiter": "dbo.Recruiters"},
        {"Recruiter": {"dbo.Recruiters", "dbo.RecruitersAlt"}},
    )
    assert _grounded_trusted_tables_for_term("recruiters", grounding) == {"dbo.Recruiters", "dbo.RecruitersAlt"}


def test_multiword_synonym_matches_via_raw_term_text():
    grounding = _grounding({"Job Order": "dbo.JobOrders"}, {"Job Order": {"dbo.JobOrders"}})
    assert _grounded_trusted_tables_for_term("job orders", grounding) == {"dbo.JobOrders"}


def test_narrow_restricts_to_trusted_tables_present_in_pool():
    table_contexts = {"dbo.Recruiters": {"table": {}}, "dbo.SomeOtherTable": {"table": {}}}
    grounding = _grounding({"Recruiter": "dbo.Recruiters"}, {"Recruiter": {"dbo.Recruiters"}})
    result = _narrow_to_grounded_table("recruiters", table_contexts, grounding)
    assert set(result.keys()) == {"dbo.Recruiters"}


def test_narrow_keeps_trusted_alternate_alongside_canonical():
    """The exact mechanism that must survive: Day 2A's bounded status-retry
    needs a real second candidate for the SAME entity, not just the
    canonical table alone."""
    table_contexts = {
        "dbo.JobOrders": {"table": {}},
        "dbo.job_orders_withStatus": {"table": {}},
        "dbo.UnrelatedTable": {"table": {}},
    }
    grounding = _grounding(
        {"Job Order": "dbo.JobOrders"},
        {"Job Order": {"dbo.JobOrders", "dbo.job_orders_withStatus"}},
    )
    result = _narrow_to_grounded_table("job orders", table_contexts, grounding)
    assert set(result.keys()) == {"dbo.JobOrders", "dbo.job_orders_withStatus"}


def test_narrow_falls_back_to_full_pool_when_no_trusted_table_present():
    """None of the entity's trusted tables made it into this particular
    call's pool (e.g. a stale/unrelated candidate set) — never narrow to
    an empty dict, which would make the term unresolvable outright."""
    table_contexts = {"dbo.SomeOtherTable": {"table": {}}}
    grounding = _grounding({"Recruiter": "dbo.Recruiters"}, {"Recruiter": {"dbo.Recruiters"}})
    result = _narrow_to_grounded_table("recruiters", table_contexts, grounding)
    assert result is table_contexts


def test_narrow_is_a_noop_when_term_matches_no_entity():
    table_contexts = {"dbo.Widgets": {"table": {}}}
    grounding = _grounding({"Recruiter": "dbo.Recruiters"}, {"Recruiter": {"dbo.Recruiters"}})
    result = _narrow_to_grounded_table("widgets", table_contexts, grounding)
    assert result is table_contexts


def test_narrow_is_a_noop_without_grounding():
    table_contexts = {"dbo.Widgets": {"table": {}}}
    result = _narrow_to_grounded_table("widgets", table_contexts, None)
    assert result is table_contexts
