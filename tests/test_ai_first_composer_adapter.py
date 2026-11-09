"""
Focused unit tests — Phase 7, Step 4, Part 6: the pure
AiFirstOrchestrationResult -> /v1/composer/ask response adapter
(api/v1/ai_first_composer_adapter.py).

Every AiFirstOrchestrationResult here is hand-built via _result(), never
produced by actually running the pipeline — mirrors tests/test_sql_pipeline_
composer_adapter.py's own convention exactly. No database, no provider, no
network call, no route, anywhere in this file.

Run:
    venv/Scripts/pytest tests/test_ai_first_composer_adapter.py -v
"""
from __future__ import annotations

import json

import pytest

from api.v1.ai_first_composer_adapter import build_ai_first_composer_response
from api.v1.query_response_adapter import NOT_PREPARED_MESSAGE
from core.sqlservice.ai_first_result import (
    STATUS_ANSWER_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_CONTEXT_BUILD_FAILED,
    STATUS_EXECUTION_FAILED,
    STATUS_GENERATION_FAILED,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RELATIONSHIP_COMPLETION_FAILED,
    STATUS_RETRIEVAL_FAILED,
    STATUS_SELECTION_FAILED,
    STATUS_VALIDATION_FAILED,
    AiFirstOrchestrationResult,
    AiFirstStageDurations,
)
from core.sqlservice.answer_generation_service import AnswerInsight, EvidenceColumnReference

SOURCE_ID = 1
SESSION_ID = "session-1"
REQUEST_ID = "request-1"


def _durations(**overrides) -> AiFirstStageDurations:
    base = dict(
        frontier_ms=None, selection_ms=None, relationship_ms=None, context_ms=None,
        ambiguity_guard_ms=None, generation_ms=None, validation_ms=None,
        execution_ms=None, answer_ms=None, total_ms=5,
    )
    base.update(overrides)
    return AiFirstStageDurations(**base)


def _result(status: str = STATUS_OK, **overrides) -> AiFirstOrchestrationResult:
    base = dict(
        status=status,
        source_id=SOURCE_ID,
        build_id=10,
        snapshot_id=20,
        schema_fingerprint="fp-1",
        answer="",
        insights=(),
        caveats=(),
        follow_up_questions=(),
        clarification_question=None,
        interpretation=None,
        sql=None,
        canonical_referenced_object_fqns=(),
        columns=(),
        rows=(),
        row_count=0,
        truncated=False,
        truncation_reason=None,
        retrieval_documents=(),
        embedding_provider_id="fake-embed",
        embedding_model="fake-embed-model",
        frontier_size=0,
        selector_status=None,
        selected_object_fqns=(),
        connector_object_fqns=(),
        selector_provider_id="fake-selector",
        selector_model="fake-selector-model",
        sql_provider_id="fake-sql",
        sql_model="fake-sql-model",
        answer_provider_id="fake-answer",
        answer_model="fake-answer-model",
        warnings=(),
        error=None,
        error_category=None,
        durations=_durations(),
    )
    base.update(overrides)
    return AiFirstOrchestrationResult(**base)


def _call(result, **overrides):
    kwargs = dict(
        result=result, session_id=SESSION_ID,
        request_id=REQUEST_ID, execution_time=0.5, conversation_state=None,
    )
    kwargs.update(overrides)
    return build_ai_first_composer_response(**kwargs)


_EXPECTED_TOP_LEVEL_KEYS = {
    "status", "request_id", "session_id", "business_answer", "resolved_intent",
    "services_selected", "evidence_summary", "evidence_package", "governance_state",
    "confidence", "execution_time", "warnings", "errors", "enterprise_answer",
    "execution_strategy", "agent_status", "pipeline_status", "agent_trace",
    "conversation_state", "perf_trace",
}


def test_top_level_keys_present():
    body = _call(_result(STATUS_OK))
    assert _EXPECTED_TOP_LEVEL_KEYS.issubset(body.keys())


# ── Truthful observability labels ──────────────────────────────────────────


def test_planning_stage_labeled_ai_query_planning_not_schema_selection():
    result = _result(
        STATUS_OK,
        durations=_durations(frontier_ms=100, selection_ms=1200, validation_ms=1, execution_ms=50, total_ms=1400),
    )
    trace_tools = [step["tool"] for step in _call(result)["agent_trace"]]
    assert "ai_query_planning" in trace_tools
    assert "schema_selection" not in trace_tools
    assert "sql_review" not in trace_tools  # no reviewer stage exists any more


def test_resolved_intent_is_a_fixed_non_classified_constant():
    body = _call(_result(STATUS_OK))
    assert body["resolved_intent"]["intent_type"] == "sql_request"
    assert body["resolved_intent"]["keywords_matched"] == []
    assert body["confidence"] == 0.0


def test_plan_cache_status_hit_miss_and_not_applicable():
    hit = _result(STATUS_OK, cache_hit=True,
                  durations=_durations(frontier_ms=0, selection_ms=0, validation_ms=1, execution_ms=50, total_ms=55))
    miss = _result(STATUS_OK, cache_hit=False,
                    durations=_durations(frontier_ms=100, selection_ms=1200, validation_ms=1, execution_ms=50, total_ms=1400))
    legacy = _result(STATUS_OK)  # cache_hit defaults to None -- no cache concept applies

    assert _call(hit)["plan_cache_status"] == "HIT"
    assert _call(miss)["plan_cache_status"] == "MISS"
    body_legacy = _call(legacy)
    assert body_legacy["plan_cache_status"] is None
    assert body_legacy["plan_cache_note"] is None


def test_plan_cache_hit_note_states_sql_still_executed_live():
    hit = _result(STATUS_OK, cache_hit=True,
                  durations=_durations(frontier_ms=0, selection_ms=0, validation_ms=1, execution_ms=50, total_ms=55))
    note = _call(hit)["plan_cache_note"]
    assert "executed live" in note
    assert "revalidated" in note
    # Never phrased as if the DATA (rows) came from cache -- only the plan did.
    assert "results" not in note.lower() and "rows" not in note.lower() and "data came from cache" not in note.lower()


def test_plan_cache_miss_note_mentions_one_embedding_and_one_planning_call():
    miss = _result(STATUS_OK, cache_hit=False,
                    durations=_durations(frontier_ms=100, selection_ms=1200, validation_ms=1, execution_ms=50, total_ms=1400))
    note = _call(miss)["plan_cache_note"]
    assert "one embedding call" in note.lower()
    assert "one ai query-planning call" in note.lower() or "one query-planning call" in note.lower()


def test_observability_fields_never_leak_sql_text_or_prompts():
    secret_sql = "SELECT CreditCardNumber, Ssn FROM dnnuser.Users WHERE 1=1"
    result = _result(
        STATUS_OK, cache_hit=True,
        sql=secret_sql,
        durations=_durations(frontier_ms=0, selection_ms=0, validation_ms=1, execution_ms=50, total_ms=55),
    )
    body = _call(result)
    observability_blob = json.dumps({
        "agent_trace": body["agent_trace"],
        "perf_trace": body["perf_trace"],
        "plan_cache_status": body["plan_cache_status"],
        "plan_cache_note": body["plan_cache_note"],
    })
    assert "CreditCardNumber" not in observability_blob
    assert "Ssn" not in observability_blob
    assert secret_sql not in observability_blob


def test_successful_answer():
    result = _result(
        STATUS_OK, answer="There are 42 students.", interpretation="Counts students.",
        sql="SELECT COUNT(*) FROM dbo.students", columns=("count",), rows=((42,),), row_count=1,
        canonical_referenced_object_fqns=("dbo.students",),
        durations=_durations(
            frontier_ms=5, selection_ms=4, relationship_ms=1, context_ms=2, ambiguity_guard_ms=1,
            generation_ms=8, validation_ms=1, execution_ms=6, answer_ms=10, total_ms=44,
        ),
    )
    body = _call(result)

    assert body["status"] == "success"
    assert body["agent_status"] == "ok"
    assert body["pipeline_status"] == "OK"
    ea = body["enterprise_answer"]
    assert ea["answer"] == "There are 42 students."
    assert ea["summary"] == "Counts students."
    assert ea["clarification"] is None
    assert ea["result_preview"] == [{"count": 42}]
    assert "sql" not in body  # SQL text is never exposed to the composer response
    json.dumps(body)  # every field must be JSON-safe


def test_clarification_required():
    result = _result(
        STATUS_CLARIFICATION_REQUIRED,
        clarification_question="This could mean tuition or IPLAN installment -- which one did you mean?",
    )
    body = _call(result)

    assert body["agent_status"] == "clarification_required"
    assert body["pipeline_status"] == "CLARIFICATION_REQUIRED"
    ea = body["enterprise_answer"]
    assert ea["answer"] == "This could mean tuition or IPLAN installment -- which one did you mean?"
    assert ea["clarification"] == {
        "reason": "This could mean tuition or IPLAN installment -- which one did you mean?",
        "options": [],
    }
    assert ea["answer_type"] == "clarification_needed"
    json.dumps(body)


def test_not_prepared():
    result = _result(STATUS_NOT_PREPARED, error="No READY retrieval build exists for source_id=1.")
    body = _call(result)
    assert body["agent_status"] == "not_prepared"
    assert body["pipeline_status"] == "NOT_PREPARED"
    assert body["enterprise_answer"]["answer"] == NOT_PREPARED_MESSAGE


@pytest.mark.parametrize(
    "status,expected_agent_status,expected_pipeline_status",
    [
        (STATUS_RETRIEVAL_FAILED, "retrieval_failed", "RETRIEVAL_FAILED"),
        (STATUS_SELECTION_FAILED, "selection_failed", "SELECTION_FAILED"),
        (STATUS_RELATIONSHIP_COMPLETION_FAILED, "relationship_completion_failed", "RELATIONSHIP_COMPLETION_FAILED"),
        (STATUS_CONTEXT_BUILD_FAILED, "context_build_failed", "CONTEXT_BUILD_FAILED"),
        (STATUS_GENERATION_FAILED, "generation_failed", "GENERATION_FAILED"),
        (STATUS_VALIDATION_FAILED, "validation_failed", "VALIDATION_FAILED"),
        (STATUS_EXECUTION_FAILED, "execution_failed", "EXECUTION_FAILED"),
        (STATUS_ANSWER_FAILED, "answer_failed", "ANSWER_FAILED"),
        (STATUS_BUDGET_EXCEEDED, "timeout", "TIMEOUT"),
    ],
)
def test_every_status_maps_deterministically(status, expected_agent_status, expected_pipeline_status):
    result = _result(status, error="a sanitized error message")
    body = _call(result)
    assert body["agent_status"] == expected_agent_status
    assert body["pipeline_status"] == expected_pipeline_status
    assert body["errors"] == ["a sanitized error message"]
    json.dumps(body)


def test_zero_rows_distinguished_from_error():
    ok_result = _result(STATUS_OK, answer="No students matched.", columns=("id",), rows=(), row_count=0)
    error_result = _result(STATUS_EXECUTION_FAILED, error="The query failed during execution.")

    ok_body = _call(ok_result)
    error_body = _call(error_result)

    assert ok_body["pipeline_status"] == "OK"
    assert ok_body["enterprise_answer"]["result_preview"] == []
    assert not ok_body["errors"]

    assert error_body["pipeline_status"] == "EXECUTION_FAILED"
    assert error_body["errors"] == ["The query failed during execution."]


def test_insights_preserved_and_grounded():
    insight = AnswerInsight(
        title="Highest enrollment", finding="Program X has the most students.",
        evidence_row_numbers=(1,), evidence_columns=(EvidenceColumnReference(position=0, name="program"),),
    )
    result = _result(STATUS_OK, insights=(insight,), columns=("program",), rows=(("X",),), row_count=1)
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["insight"] is None  # singular slot never force-fit, matches existing adapter contract
    assert len(ea["insights"]) == 1
    assert ea["insights"][0]["title"] == "Highest enrollment"
    citation_refs = {c["reference"] for c in ea["citations"]}
    assert "program" in citation_refs


def test_scalar_count_produces_kpi_not_chart_or_forced_narrative():
    # Phase 7, Step 6G, Part 6 -- "scalar count -> KPI, no forced narrative".
    result = _result(
        STATUS_OK, answer="There are 214 students.", columns=("StudentCount",), rows=((214,),), row_count=1,
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["kpi"] == {"label": "Student Count", "value": 214}
    assert ea["chart"] is None
    assert ea["insights"] == []  # a single scalar has nothing to compare -- no fabricated commentary
    json.dumps(body)


def test_monthly_trend_produces_line_chart():
    # Phase 7, Step 6G, Part 6 -- "monthly trend -> line chart and trend insight".
    result = _result(
        STATUS_OK, answer="Enrollment by month.",
        columns=("Month", "EnrollmentCount"),
        rows=(("2026-01", 40), ("2026-02", 55), ("2026-03", 38)), row_count=3,
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"]["chart_type"] == "line"
    assert ea["chart"]["labels"] == ["2026-01", "2026-02", "2026-03"]
    assert ea["kpi"] is None
    json.dumps(body)


def test_category_totals_produce_bar_chart():
    # Phase 7, Step 6G, Part 6 -- "category totals -> bar chart and highest/lowest insight".
    # 8 categories is past the small-composition donut threshold, so this
    # deterministically resolves to "bar" (see test_small_non_negative_
    # composition_produces_donut_chart in test_sqlservice_result_chart_
    # builder.py for the <=6-category donut case).
    result = _result(
        STATUS_OK, answer="Students by program.",
        columns=("Program", "StudentCount"),
        rows=(
            ("Data Analytics", 120), ("UX Design", 80), ("Data Engineering", 45), ("AI", 60),
            ("Cloud", 30), ("Cyber", 25), ("DevOps", 15), ("QA", 10),
        ), row_count=8,
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"]["chart_type"] == "bar"
    json.dumps(body)


def test_zero_rows_produces_no_chart_no_kpi():
    result = _result(STATUS_OK, answer="No students matched.", columns=("id",), rows=(), row_count=0)
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"] is None
    assert ea["kpi"] is None


def test_one_row_multi_column_detail_produces_no_chart_no_kpi_table_only():
    # PII-heavy detail result -- names + class -- table only, matching
    # Part 3's "detailed records or PII-heavy results -> table only".
    result = _result(
        STATUS_OK, answer="Found 1 matching record.",
        columns=("FirstName", "LastName", "ClassName"),
        rows=(("Ada", "Lovelace", "SQL 101"),), row_count=1,
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"] is None
    assert ea["kpi"] is None


def test_truncated_result_never_produces_chart_or_kpi():
    # Never implies a chart/KPI represents the full population when the
    # result itself was truncated.
    result = _result(
        STATUS_OK, answer="Found matching records.",
        columns=("Program", "StudentCount"),
        rows=(("A", 500), ("B", 500)), row_count=2, truncated=True, truncation_reason="row cap",
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"] is None
    assert ea["kpi"] is None
    assert ea["truncation_notice"] is not None


def test_ambiguous_multi_column_result_produces_no_chart():
    result = _result(
        STATUS_OK, answer="Breakdown by program.",
        columns=("Program", "Enrolled", "Graduated"),
        rows=(("A", 10, 5), ("B", 20, 15)), row_count=2,
    )
    body = _call(result)
    assert body["enterprise_answer"]["chart"] is None


def test_chart_never_contains_sql_text_or_credentials():
    result = _result(
        STATUS_OK, answer="Enrollment by month.", sql="SELECT Month, COUNT(*) FROM dbo.EDU_Student",
        columns=("Month", "EnrollmentCount"),
        rows=(("2026-01", 40), ("2026-02", 55)), row_count=2,
    )
    body = _call(result)
    chart_json = json.dumps(body["enterprise_answer"]["chart"])
    assert "SELECT" not in chart_json
    assert "EDU_Student" not in chart_json


def test_secret_bearing_rejection_never_reaches_chart_or_insight_builders():
    # Phase 7, Step 6G, Part 6 -- "secret-bearing output rejected before
    # insight or chart generation". A validation-stage rejection (secret
    # detection lives in sql_validation/execution, untouched by this step)
    # leaves execution_result's columns/rows/row_count at their failure
    # defaults ((), (), 0) -- the chart/kpi/insight builders only ever see
    # that empty shape and can never fabricate output from what was
    # actually, separately rejected.
    result = _result(
        STATUS_VALIDATION_FAILED,
        error="SQL rejected: candidate column references a secret-bearing object.",
    )
    body = _call(result)
    ea = body["enterprise_answer"]
    assert ea["chart"] is None
    assert ea["kpi"] is None
    assert ea["insights"] == []
    assert body["pipeline_status"] == "VALIDATION_FAILED"


def test_agent_trace_marks_failing_stage():
    result = _result(
        STATUS_VALIDATION_FAILED, error="SQL rejected.",
        durations=_durations(frontier_ms=5, selection_ms=4, relationship_ms=1, context_ms=2,
                              ambiguity_guard_ms=1, generation_ms=8, validation_ms=3, total_ms=41),
    )
    body = _call(result)
    trace_by_tool = {step["tool"]: step for step in body["agent_trace"]}
    assert trace_by_tool["sql_validation"]["status"] == "failed"
    assert trace_by_tool["sql_generation"]["status"] == "ok"
    assert "sql_execution" not in trace_by_tool  # never reached, so never traced


def test_transient_provider_failure_suggests_retry():
    result = _result(
        STATUS_SELECTION_FAILED, error="The fast-path combined generation provider call failed.", error_category="provider",
    )
    body = _call(result)
    assert "try asking again" in body["enterprise_answer"]["answer"].lower()


def test_non_transient_failure_does_not_suggest_retry():
    result = _result(
        STATUS_VALIDATION_FAILED, error="Referenced column does not exist.", error_category="invalid_response",
    )
    body = _call(result)
    assert "try asking again" not in body["enterprise_answer"]["answer"].lower()


def test_perf_trace_maps_frontier_to_retrieval():
    result = _result(STATUS_OK, durations=_durations(frontier_ms=12, total_ms=12))
    body = _call(result)
    assert body["perf_trace"]["retrieval_ms"] == 12


# ── Phase 7, Step 6F, Part 6 — follow_up_resolution presentation field ──


def test_follow_up_resolution_absent_for_a_fresh_question():
    result = _result(STATUS_OK, resolved_standalone_question=None, follow_up_resolution_type=None)
    body = _call(result)
    assert body["follow_up_resolution"] == {"resolution_type": None, "resolved_standalone_question": None}


def test_follow_up_resolution_present_for_a_resolved_continuation():
    result = _result(
        STATUS_OK, resolved_standalone_question="How many active widgets are there?",
        follow_up_resolution_type="continuation",
    )
    body = _call(result)
    assert body["follow_up_resolution"] == {
        "resolution_type": "continuation", "resolved_standalone_question": "How many active widgets are there?",
    }


def test_follow_up_resolution_never_leaks_row_values(monkeypatch=None):
    result = _result(
        STATUS_OK, resolved_standalone_question="Only active widgets.", follow_up_resolution_type="continuation",
        rows=(("Acme Corp", 42),), columns=("name", "count"),
    )
    body = _call(result)
    serialized = json.dumps(body["follow_up_resolution"])
    assert "Acme Corp" not in serialized
