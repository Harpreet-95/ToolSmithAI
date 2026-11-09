"""
Day 2E, Task 3 — Deterministic contract selection.

Live-verified defect: "How many launch participants are stalled, active,
graduated, or not started?" sometimes answered correctly (via the curated
status view) and sometimes SAFELY_REFUSED on an immediately-repeated call —
because _find_sufficient_single_object (the status-enumeration shortcut)
ran BEFORE grounding was even computed and used its own independent,
broad-search-backed discovery, completely unrelated to the grounded
entity's own contract.

This file proves: for identical source_id/question/grounding, the selected
object and full logical plan are byte-identical across repeated calls, and
the status-enumeration path never touches the broad AI-ranked search once
the named entity is grounded.

Run:
    venv/Scripts/pytest tests/test_deterministic_contract_selection.py -v
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-day2e-deterministic-contract-secret-long-en1")
os.environ.setdefault("USER_ID_SALT", "test-day2e-deterministic-contract-salt-long-eno12")

import data.semantic_contract_service as scs
import data.query_planning_service as qps
from data.sql_planning_service import build_sql_plan
from data.sql_generation_service import generate_sql, detect_dialect
from tests.test_phase9_query_planning import env, _add_table, _add_column, _add_table_type, _add_value_samples


def _launch_participant_status_grounding():
    return {
        "matched_entities": ["Pilot Participant"],
        "grounded": {"Pilot Participant": {
            "canonical_table_fqn": "dbo.EDU_Pilot_Participants",
            "preferred_view_fqn": "dbo.v_pilot_participant_current_status",
            "contract": {"alternate_candidates": ["dbo.EDU_Student"], "status_semantics": None},
        }},
        "unsupported": {}, "fully_grounded": True,
        "seed_table_fqns": {
            "dbo.EDU_Pilot_Participants", "dbo.v_pilot_participant_current_status", "dbo.EDU_Student",
        },
        "entity_table_map": {"Pilot Participant": "dbo.EDU_Pilot_Participants"},
        "entity_trusted_tables": {"Pilot Participant": {
            "dbo.EDU_Pilot_Participants", "dbo.EDU_Student",
        }},
        "status_hints": {},
    }


def _seed_db(tmp_path, monkeypatch):
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.EDU_Pilot_Participants", table_class="Transactional", row_count=500)
    _add_column(db, "dbo.EDU_Pilot_Participants", "ParticipantID", is_pk=1, is_id=1, uniqueness=1.0)

    _add_table(db, "dbo.v_pilot_participant_current_status", table_class="Master", row_count=500)
    _add_table_type(db, "dbo.v_pilot_participant_current_status", "VIEW")
    _add_column(db, "dbo.v_pilot_participant_current_status", "ParticipantID", is_pk=1, is_id=1, uniqueness=1.0)
    status_col_id = _add_column(
        db, "dbo.v_pilot_participant_current_status", "Current_Status", data_type="TEXT",
        is_dimension=True, business_label="Current Status", approved=True,
    )
    _add_value_samples(db, status_col_id, ["Active", "Graduated", "Stalled", "Not Started"])

    _add_table(db, "dbo.EDU_Student", table_class="Master", row_count=9000)
    _add_column(db, "dbo.EDU_Student", "StudentID", is_pk=1, is_id=1, uniqueness=1.0)
    return db


def test_status_enumeration_never_calls_broad_search_when_grounded(tmp_path, monkeypatch):
    grounding = _launch_participant_status_grounding()
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: grounding)

    calls = []
    real_retrieval = qps._get_ai_candidate_tables_ranked

    def spy_retrieval(*a, **k):
        calls.append(1)
        return real_retrieval(*a, **k)

    monkeypatch.setattr(qps, "_get_ai_candidate_tables_ranked", spy_retrieval)

    question = "How many launch participants are stalled, active, graduated, or not started?"
    result = qps.plan_business_query(1, "u1", {
        "question": question, "concepts": ["launch", "participants"],
        "measures": ["launch", "participants"], "dimensions": [], "filters": [],
    })
    assert calls == []  # the broad AI-ranked search was never invoked
    assert result.get("sufficiency_shortcut") is not None
    assert result["sufficiency_shortcut"]["table_fqn"] == "dbo.v_pilot_participant_current_status"


def test_repeated_runs_produce_byte_identical_plan_and_sql(tmp_path, monkeypatch):
    grounding = _launch_participant_status_grounding()
    _seed_db(tmp_path, monkeypatch)
    monkeypatch.setattr(scs, "apply_grounding", lambda *a, **k: grounding)

    question = "How many launch participants are stalled, active, graduated, or not started?"

    def run_once():
        qp = qps.plan_business_query(1, "u1", {
            "question": question, "concepts": ["launch", "participants"],
            "measures": ["launch", "participants"], "dimensions": [], "filters": [],
        })
        sp = build_sql_plan(1, "u1", qp)
        gen = generate_sql(1, "u1", sp, dialect=detect_dialect(1)) if sp["validation"]["valid"] else None
        return {
            "primary_table": (qp.get("join_plan") or {}).get("primary_table"),
            "select": sp["select"], "where": sp["where"], "group_by": sp["group_by"],
            "sql": gen["sql"] if gen else None,
        }

    results = [run_once() for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert results[0]["primary_table"] == "dbo.v_pilot_participant_current_status"
