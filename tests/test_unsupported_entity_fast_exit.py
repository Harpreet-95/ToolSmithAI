"""
Day 2D, Priority 2 — fast exit for unsupported entities.

Confirmed live problem: "Recruiter" and "Placement" have no valid semantic
contract (confirmed NO_SAFE_SELECTION), but every question mentioning them
still pays the full ~30-40s broad-search cost before refusing, on every ask.

Root cause: data.semantic_contract_service.apply_grounding already fetches
each matched entity's contract via get_or_build_entity_contract (a fast,
already-cached read for a fresh metadata_revision, including for a
NO_SAFE_SELECTION result) — but only records an entity into `grounded` when
RESOLVED/PARTIAL, silently dropping a confirmed-unsupported match from its
return value entirely. data.query_planning_service.plan_business_query then
branches on `fully_grounded`: since an unsupported entity never appears in
`grounded`, fully_grounded is False, so it falls into the broad-retrieval
branch (_get_ai_candidate_tables_ranked) unconditionally — the actual cost,
run every single time regardless of the fast, already-known-negative
contract sitting right there.

This mirrors tests/test_semantic_contract_planner_integration.py's own
fake-grounding + retrieval-spy pattern.

Run:
    venv/Scripts/pytest tests/test_unsupported_entity_fast_exit.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2d-unsupported-entity-secret-long-enough1")
os.environ.setdefault("USER_ID_SALT", "test-day2d-unsupported-entity-salt-long-enough12")

import data.semantic_contract_service as scs
import data.query_planning_service as qps
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_domain


def _plan(question, concepts, measures, dimensions=None):
    return qps.plan_business_query(1, "u1", {
        "question": question, "concepts": concepts, "measures": measures,
        "dimensions": dimensions or [],
    })


def _no_safe_selection_contract(entity_name, *, evidence=None):
    return {
        "canonical_table_fqn": None, "preferred_view_fqn": None,
        "resolution_status": "NO_SAFE_SELECTION", "confidence": 0.0,
        "contract": {"evidence": evidence or [f"No reliable canonical table found for {entity_name}."]},
        "metadata_revision": "rev-1",
        "generation_method": "ai_adjudicated",
    }


def test_fully_unsupported_question_skips_broad_retrieval_entirely(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.SomeOtherTable", table_class="Transactional")
    _add_column(db, "dbo.SomeOtherTable", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)

    fake_grounding = {
        "matched_entities": ["Recruiter", "Placement"],
        "grounded": {},
        "unsupported": {
            "Recruiter": _no_safe_selection_contract("Recruiter"),
            "Placement": _no_safe_selection_contract("Placement"),
        },
        "fully_grounded": False,
        "seed_table_fqns": set(),
        "entity_table_map": {},
        "entity_trusted_tables": {},
        "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    monkeypatch.setattr(
        qps, "_get_ai_candidate_tables_ranked",
        lambda *a, **k: calls.append(1) or (set(), [], []),
    )

    result = _plan(
        "Show recruiters and their placements", ["recruiters", "placements"], ["recruiters", "placements"],
    )
    assert calls == []  # broad retrieval never called
    assert result is not None
    assert result["measures"] == [] and result["dimensions"] == []
    assert result["unsupported_entities"] == ["Placement", "Recruiter"]
    assert any(w["type"] == "unsupported_entity" and "Recruiter" in w["message"] for w in result["warnings"])
    assert any(w["type"] == "unsupported_entity" and "Placement" in w["message"] for w in result["warnings"])


def test_partially_unsupported_mixed_question_returns_targeted_refusal(tmp_path, monkeypatch):
    """'Show candidates linked to active placements' — Candidate grounds
    fine, Placement doesn't. Product decision (Day 3, Task 6): fast-exit
    with a targeted, table-free business refusal that preserves the
    grounded entity and explains only the missing one — never fall back to
    the broad-search/physical-table-picker path for the unresolved entity.
    Physical detail (table candidates, resolution_status) is still allowed
    in `warnings` for developer diagnostics; it must never appear in the
    user-facing `business_messages`.

    Supersedes this test's own prior assertion (pre-Day-3) that a partial
    match must NOT fast-exit — that was the correct behavior before the
    product goal changed to always avoid exposing a physical-table
    clarification for an unsupported entity, even when a sibling entity in
    the same question is grounded fine.
    """
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Candidates", table_class="Transactional")
    _add_column(db, "dbo.Candidates", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Candidates", "Operations")

    fake_grounding = {
        "matched_entities": ["Candidate", "Placement"],
        "grounded": {"Candidate": {"canonical_table_fqn": "dbo.Candidates"}},
        "unsupported": {"Placement": _no_safe_selection_contract("Placement")},
        "fully_grounded": False,
        "seed_table_fqns": {"dbo.Candidates"},
        "entity_table_map": {"Candidate": "dbo.Candidates"},
        "entity_trusted_tables": {"Candidate": {"dbo.Candidates"}},
        "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    result = _plan(
        "Show candidates linked to active placements", ["candidates", "placements"], ["candidates", "placements"],
    )
    assert calls == []  # broad retrieval never called — fast-exited on the targeted refusal
    assert result is not None
    assert result["unsupported_entities"] == ["Placement"]
    assert result["grounded_entities"] == ["Candidate"]
    # Grounded entity is preserved (named), missing one is explained — both table-free.
    assert any("candidate" in m.lower() for m in result["business_messages"])
    assert any("placement" in m.lower() for m in result["business_messages"])
    assert not any("dbo." in m for m in result["business_messages"])
    # Physical detail is still permitted in warnings, for developer diagnostics only.
    assert any(w["type"] == "unsupported_entity" and "Placement" in w["message"] for w in result["warnings"])


def test_no_entity_match_leaves_ordinary_path_untouched(tmp_path, monkeypatch):
    """A question naming no taxonomy entity at all must never trip the
    fast-exit — grounding["matched_entities"] is empty, not "all unsupported"."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.Widgets", table_class="Transactional")
    _add_column(db, "dbo.Widgets", "id", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_domain(db, "dbo.Widgets", "Operations")

    fake_grounding = {
        "matched_entities": [], "grounded": {}, "unsupported": {},
        "fully_grounded": False, "seed_table_fqns": set(),
        "entity_table_map": {}, "entity_trusted_tables": {}, "status_hints": {},
    }
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: fake_grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    result = _plan("Show widgets", ["widgets"], ["widgets"])
    assert calls == [1]
    assert "unsupported_entities" not in result
