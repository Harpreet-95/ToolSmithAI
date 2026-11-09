"""
RETIRE-LEGACY-FALLBACK MILESTONE, Part 17 — observability.

core.observability.pipeline_trace.build_pipeline_trace() previously never
received its own `ai_used` parameter from any real caller (audited: the sole
call site, data.query_execution_service.execute_governed_query, always
called it as build_pipeline_trace(sql_plan) with no second argument), so the
persisted trace's `ai_planning` section was always None/unknown regardless
of which planner actually answered a question. This milestone wires a real
caller (core.orchestrator.agent's execute_governed_query call) and adds the
exact fields Part 17 requires, most importantly `legacy_semantic_planner`
(must be False for every normal Q&A request unless a genuine competing
fallback occurred — see AgentState.legacy_semantic_planner_competing_fallback
for the authoritative definition).

Run:
    python -m pytest tests/test_pipeline_trace_legacy_planner_field.py -v
"""
from __future__ import annotations

from core.observability.pipeline_trace import build_pipeline_trace

_MINIMAL_SQL_PLAN = {
    "intent": {"type": "entity_count", "aggregation": "COUNT"},
    "from": {"table_fqn": "dbo.orders"},
    "joins": [],
    "semantic_context": [],
    "validation": {"valid": True, "checks": {}, "blocking_reasons": []},
    "warnings": [],
    "explanation": [],
}


def test_ai_used_none_leaves_new_fields_unknown_not_false():
    trace = build_pipeline_trace(_MINIMAL_SQL_PLAN)
    ai_planning = trace["ai_planning"]
    # None (unknown), never False — a caller that doesn't report anything
    # must never be silently read as "legacy definitely did not run".
    assert ai_planning["legacy_semantic_planner"] is None
    assert ai_planning["physical_planner_attempted"] is None
    assert ai_planning["physical_planner_valid"] is None
    assert ai_planning["ai_interpretation_deliberately_skipped"] is None
    assert ai_planning["legacy_fallback_reason"] is None


def test_ai_used_threads_legacy_semantic_planner_field_through():
    ai_used = {
        "question_interpreter": True,
        "physical_planner_attempted": True,
        "physical_planner_valid": True,
        "candidate_adjudicator": True,
        "ai_interpretation_deliberately_skipped": False,
        "legacy_semantic_planner": False,
        "legacy_fallback_reason": None,
    }
    trace = build_pipeline_trace(_MINIMAL_SQL_PLAN, ai_used=ai_used)
    ai_planning = trace["ai_planning"]
    assert ai_planning["legacy_semantic_planner"] is False
    assert ai_planning["physical_planner_attempted"] is True
    assert ai_planning["physical_planner_valid"] is True
    assert ai_planning["question_interpreter_used"] is True
    assert ai_planning["candidate_adjudicator_used"] is True


def test_ai_used_can_report_deliberate_skip_without_legacy_competing_fallback():
    # The "safe" legacy case this milestone preserves: deterministic
    # grounding resolved the question, AI was never consulted, and
    # legacy_semantic_planner (the competing-fallback metric) is still
    # correctly False.
    ai_used = {
        "question_interpreter": False,
        "physical_planner_attempted": False,
        "physical_planner_valid": False,
        "candidate_adjudicator": False,
        "ai_interpretation_deliberately_skipped": True,
        "legacy_semantic_planner": False,
        "legacy_fallback_reason": "no_ai_interpretation",
    }
    trace = build_pipeline_trace(_MINIMAL_SQL_PLAN, ai_used=ai_used)
    ai_planning = trace["ai_planning"]
    assert ai_planning["ai_interpretation_deliberately_skipped"] is True
    assert ai_planning["legacy_semantic_planner"] is False
    assert ai_planning["legacy_fallback_reason"] == "no_ai_interpretation"


def test_never_includes_raw_sql_or_row_data():
    sql_plan = dict(_MINIMAL_SQL_PLAN)
    sql_plan["generated_sql_text_should_never_appear"] = "SELECT * FROM secrets"
    trace = build_pipeline_trace(sql_plan, ai_used={"legacy_semantic_planner": False})
    serialized = str(trace)
    assert "SELECT * FROM secrets" not in serialized
