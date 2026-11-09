"""
AI-first fast-path Composer adapter -- Phase 8A.

Pure translation of core.sqlservice.ai_first_result.
AiFirstOrchestrationResult (the fast path's terminal result) into the JSON
response shape POST /v1/composer/ask returns -- the contract
frontend/src/components/AIWorkspace.jsx renders (EnterpriseAnswerBlock /
ClarificationCard / ResultPreviewTable / AgentTraceSection).

No business logic lives here: every field is read straight off
AiFirstOrchestrationResult (or passed through unchanged from the caller-
supplied request/session context), never recomputed, re-validated, or
re-executed. Pure and deterministic:
  - no database/provider/route call of any kind;
  - no timestamp or UUID generated here;
  - no mutation of the input (a frozen dataclass);
  - no raw internal prompt, schema-document text, embedding vector,
    credential, provider/driver error, or SQL text ever reaches the output.

Status mapping: `agent_status` (lowercase) and `pipeline_status`
(UPPERCASE, BUDGET_EXCEEDED reported as TIMEOUT) are derived 1:1 from the
result's own status -- an early-stage failure is never reported as a later
stage's, so a schema failure is never misreported as EXECUTION_FAILED.

enterprise_answer.chart / kpi are deterministic, zero-AI-call specs built
by core.sqlservice.result_chart_builder from this result's own columns/
rows; both are None whenever the shape does not cleanly support one.
enterprise_answer.insights are the grounded insight list. agent_trace is
built from the result's real per-stage durations; plan_cache_status/
plan_cache_note report the real plan-cache lookup outcome (a HIT never
means data came from cache -- SQL is revalidated and executed live).
"""
from __future__ import annotations

from typing import Optional

from core.answering.models import AnswerType, Citation, CitationType, EnterpriseAnswer
from core.sqlservice.result_chart_builder import build_deterministic_chart, build_deterministic_kpi
from core.execution.execution_strategy import ExecutionStrategy, StrategyType
from core.sqlservice.ai_first_result import (
    ERROR_CATEGORY_PROVIDER,
    STATUS_ANSWER_FAILED,
    STATUS_BUDGET_EXCEEDED,
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_CONTEXT_BUILD_FAILED,
    STATUS_EXECUTION_FAILED,
    STATUS_GENERATION_FAILED,
    STATUS_NON_DATA,
    STATUS_NOT_PREPARED,
    STATUS_OK,
    STATUS_RELATIONSHIP_COMPLETION_FAILED,
    STATUS_RETRIEVAL_FAILED,
    STATUS_SELECTION_FAILED,
    STATUS_UNSUPPORTED,
    STATUS_VALIDATION_FAILED,
    AiFirstOrchestrationResult,
)

from api.v1.query_response_adapter import (
    NOT_PREPARED_MESSAGE,
    _display_row_keys,
    _insight_to_dict,
    _json_safe_cell,
)

__all__ = ["build_ai_first_composer_response", "FIXED_RESOLVED_INTENT"]

# The response contract still carries `resolved_intent` (the UI's technical-
# details panel reads it), but no keyword classification runs any more: the
# request kind is decided by the fast path's own model call.
FIXED_RESOLVED_INTENT = {
    "intent_type": "sql_request",
    "confidence": 0.0,
    "required_capabilities": [],
    "secondary_intents": [],
    "keywords_matched": [],
}

_SERVICES_SELECTED = ["live_query"]
_EXECUTION_STRATEGY_IMPLEMENTATION = "sqlservice_ai_first"

_AGENT_STATUS_BY_STATUS: dict = {
    STATUS_OK: "ok",
    STATUS_NOT_PREPARED: "not_prepared",
    STATUS_RETRIEVAL_FAILED: "retrieval_failed",
    STATUS_SELECTION_FAILED: "selection_failed",
    STATUS_RELATIONSHIP_COMPLETION_FAILED: "relationship_completion_failed",
    STATUS_CONTEXT_BUILD_FAILED: "context_build_failed",
    STATUS_GENERATION_FAILED: "generation_failed",
    STATUS_CLARIFICATION_REQUIRED: "clarification_required",
    STATUS_NON_DATA: "non_data",
    STATUS_UNSUPPORTED: "unsupported",
    STATUS_VALIDATION_FAILED: "validation_failed",
    STATUS_EXECUTION_FAILED: "execution_failed",
    STATUS_ANSWER_FAILED: "answer_failed",
    STATUS_BUDGET_EXCEEDED: "timeout",
}

_PIPELINE_STATUS_BY_STATUS: dict = {
    STATUS_OK: "OK",
    STATUS_NOT_PREPARED: "NOT_PREPARED",
    STATUS_RETRIEVAL_FAILED: "RETRIEVAL_FAILED",
    STATUS_SELECTION_FAILED: "SELECTION_FAILED",
    STATUS_RELATIONSHIP_COMPLETION_FAILED: "RELATIONSHIP_COMPLETION_FAILED",
    STATUS_CONTEXT_BUILD_FAILED: "CONTEXT_BUILD_FAILED",
    STATUS_GENERATION_FAILED: "GENERATION_FAILED",
    STATUS_CLARIFICATION_REQUIRED: "CLARIFICATION_REQUIRED",
    STATUS_NON_DATA: "NON_DATA",
    STATUS_UNSUPPORTED: "UNSUPPORTED",
    STATUS_VALIDATION_FAILED: "VALIDATION_FAILED",
    STATUS_EXECUTION_FAILED: "EXECUTION_FAILED",
    STATUS_ANSWER_FAILED: "ANSWER_FAILED",
    STATUS_BUDGET_EXCEEDED: "TIMEOUT",
}

# One entry per pipeline stage that can appear in agent_trace, in stage
# order: (tool name, AiFirstStageDurations field name, the STATUS_* that
# means THIS stage is the one that failed). "ai_query_planning" is ONE
# combined call that determines intent, schema, AND SQL together; its
# perf_trace field keeps the name "selection_ms" for compatibility.
_STAGE_TRACE_SPECS = (
    ("schema_frontier", "frontier_ms", STATUS_RETRIEVAL_FAILED),
    ("ai_query_planning", "selection_ms", STATUS_SELECTION_FAILED),
    ("relationship_completion", "relationship_ms", STATUS_RELATIONSHIP_COMPLETION_FAILED),
    ("schema_context", "context_ms", STATUS_CONTEXT_BUILD_FAILED),
    ("ambiguity_guard", "ambiguity_guard_ms", None),
    ("sql_generation", "generation_ms", STATUS_GENERATION_FAILED),
    ("sql_validation", "validation_ms", STATUS_VALIDATION_FAILED),
    ("sql_execution", "execution_ms", STATUS_EXECUTION_FAILED),
    ("answer_generation", "answer_ms", STATUS_ANSWER_FAILED),
)


# ── Small pure helpers ──────────────────────────────────────────────────


# Part 7 — "show a safe retry message for transient provider failures".
# error_category == ERROR_CATEGORY_PROVIDER means the underlying SDK/
# network call itself failed (timeout, rate limit, transient 5xx) rather
# than a structural/content rejection (ERROR_CATEGORY_INVALID_RESPONSE) or
# a configuration problem (ERROR_CATEGORY_CONFIGURATION) — the one category
# where simply asking again is a genuinely reasonable next step, unlike the
# other two (retrying an invalid-response or misconfiguration failure
# unchanged would just fail the same way again).
_TRANSIENT_RETRY_SUFFIX = " This may be a temporary issue — please try asking again."


def _is_transient_provider_failure(result: AiFirstOrchestrationResult) -> bool:
    return result.error_category == ERROR_CATEGORY_PROVIDER


def _answer_text_for(result: AiFirstOrchestrationResult) -> str:
    if result.status == STATUS_OK:
        return result.answer
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        return result.clarification_question or ""
    if result.status in (STATUS_NON_DATA, STATUS_UNSUPPORTED):
        # result.answer carries the selector's own response_text for these
        # two statuses (see core.sqlservice.ai_first_question_answering_
        # service._build_result's non_data_response wiring).
        return result.answer or "This request could not be answered."
    if result.status == STATUS_NOT_PREPARED:
        return NOT_PREPARED_MESSAGE
    if result.status == STATUS_ANSWER_FAILED:
        base = result.error or "The query ran successfully, but an answer could not be generated for it."
    elif result.status == STATUS_BUDGET_EXCEEDED:
        base = result.error or "This question took too long to answer. Please try again or narrow the question."
    else:
        base = result.error or "This question could not be answered."
    if _is_transient_provider_failure(result) and result.status != STATUS_BUDGET_EXCEEDED:
        base = base + _TRANSIENT_RETRY_SUFFIX
    return base


def _limitations_for(result: AiFirstOrchestrationResult) -> list:
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        return ["No SQL has been generated or executed pending your selection."]
    if result.status == STATUS_OK:
        return list(result.caveats)
    if result.status == STATUS_ANSWER_FAILED or (
        result.status == STATUS_BUDGET_EXCEEDED and result.durations.execution_ms is not None
    ):
        return list(result.caveats) + ([result.error] if result.error else [])
    message = NOT_PREPARED_MESSAGE if result.status == STATUS_NOT_PREPARED else result.error
    return [message] if message else []


def _next_actions_for(result: AiFirstOrchestrationResult) -> list:
    if result.status == STATUS_NOT_PREPARED:
        return [NOT_PREPARED_MESSAGE]
    return []


def _truncation_notice_for(result: AiFirstOrchestrationResult) -> Optional[str]:
    if not result.truncated:
        return None
    reason = f" ({result.truncation_reason})" if result.truncation_reason else ""
    return f"Results were truncated{reason}."


def _clarification_for(result: AiFirstOrchestrationResult) -> Optional[dict]:
    if result.status != STATUS_CLARIFICATION_REQUIRED:
        return None
    # this pipeline's clarification is always one free-text question
    # (from the schema selector, relationship completion, the ambiguity
    # guard, the SQL generator, or the reviewer — never a structured
    # per-term candidate list), so options: [] is itself a contract-
    # supported state, never a fabricated options list.
    return {"reason": result.clarification_question, "options": []}


def _result_preview_for(result: AiFirstOrchestrationResult) -> list:
    display_columns = _display_row_keys(result.columns)
    return [
        {display_columns[position]: _json_safe_cell(value) for position, value in enumerate(row)}
        for row in result.rows
    ]


def _citations_for(result: AiFirstOrchestrationResult) -> list:
    citations: list = [
        Citation(source_type=CitationType.TABLE, reference=table_fqn, label=table_fqn)
        for table_fqn in result.canonical_referenced_object_fqns
    ]
    seen: set = set()
    for insight in result.insights:
        for column_ref in insight.evidence_columns:
            key = (column_ref.position, column_ref.name)
            if key in seen:
                continue
            seen.add(key)
            citations.append(
                Citation(
                    source_type=CitationType.COLUMN, reference=column_ref.name, label=column_ref.name,
                    detail={"position": column_ref.position},
                )
            )
    return citations


def _agent_trace_for(result: AiFirstOrchestrationResult) -> list:
    trace: list = []
    for step_number, (tool, field_name, failing_status) in enumerate(_STAGE_TRACE_SPECS, start=1):
        duration_ms = getattr(result.durations, field_name)
        if duration_ms is None:
            continue
        failed = failing_status is not None and result.status == failing_status
        trace.append(
            {
                "step": step_number,
                "tool": tool,
                "status": "failed" if failed else "ok",
                "input_summary": None,
                "output_summary": None,
                "reason_code": result.error_category if failed else None,
                "duration_ms": duration_ms,
            }
        )
    return trace


# Truthful, user-facing plan-cache status: "HIT"/"MISS", straight from
# AiFirstOrchestrationResult.cache_hit (itself set from the real plan-cache
# lookup, never inferred from timing). A HIT never means database results
# came from cache -- deterministic validation and live SQL execution happen
# unconditionally either way.
def _plan_cache_status_for(result: AiFirstOrchestrationResult) -> Optional[str]:
    if result.cache_hit is None:
        return None
    return "HIT" if result.cache_hit else "MISS"


def _plan_cache_note_for(result: AiFirstOrchestrationResult) -> Optional[str]:
    if result.cache_hit is None:
        return None
    if result.cache_hit:
        return (
            "The query plan (intent, schema, and SQL shape) was reused from cache. "
            "SQL was revalidated and executed live against current data for this request."
        )
    return "No matching cached query plan was found. One embedding call and one AI query-planning call were made."


def _perf_trace_for(result: AiFirstOrchestrationResult) -> dict:
    d = result.durations
    return {
        "retrieval_ms": d.frontier_ms,
        "selection_ms": d.selection_ms,
        "relationship_ms": d.relationship_ms,
        "context_ms": d.context_ms,
        "ambiguity_guard_ms": d.ambiguity_guard_ms,
        "generation_ms": d.generation_ms,
        "validation_ms": d.validation_ms,
        "execution_ms": d.execution_ms,
        "answer_ms": d.answer_ms,
        "total_ms": d.total_ms,
    }


def _evidence_summary_for(result: AiFirstOrchestrationResult) -> dict:
    return {
        "total_items": 1,
        "services_attempted": 1,
        "services_succeeded": 1 if result.status == STATUS_OK else 0,
        "has_errors": result.status not in (
            STATUS_OK, STATUS_CLARIFICATION_REQUIRED, STATUS_NON_DATA, STATUS_UNSUPPORTED,
        ),
    }


def _business_answer_for(result: AiFirstOrchestrationResult) -> dict:
    limitations = _limitations_for(result)
    next_actions = _next_actions_for(result)
    return {
        "answer": _answer_text_for(result),
        "answer_type": "live_query",
        "source_summary": f"SQL pipeline status: {result.status}.",
        "confidence": 0.0,
        "limitations": limitations,
        "next_suggested_action": next_actions[0] if next_actions else "",
    }


def _enterprise_answer_for(result: AiFirstOrchestrationResult) -> dict:
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        answer_type = AnswerType.CLARIFICATION_NEEDED
    elif result.status == STATUS_UNSUPPORTED:
        answer_type = AnswerType.RESTRICTED
    else:
        answer_type = AnswerType.LIVE_QUERY
    enterprise_answer = EnterpriseAnswer(
        answer=_answer_text_for(result),
        summary=result.interpretation or "",
        answer_type=answer_type,
        confidence=0,
        citations=_citations_for(result),
        recommendations=[],
        limitations=_limitations_for(result),
        follow_up_questions=list(result.follow_up_questions),
        next_actions=_next_actions_for(result),
        related_objects=list(result.canonical_referenced_object_fqns),
        execution_summary={
            "status": result.status,
            "row_count": result.row_count,
            "duration_ms": result.durations.total_ms,
        },
        clarification=_clarification_for(result),
        result_preview=_result_preview_for(result),
        source_tables=list(result.canonical_referenced_object_fqns),
        source_columns=list(result.columns),
        assumptions=[],
        truncation_notice=_truncation_notice_for(result),
        insight=None,
        chart=build_deterministic_chart(result),
        insights=[_insight_to_dict(i) for i in result.insights],
        kpi=build_deterministic_kpi(result),
        data_quality_notes=[],
        technical_diagnostics=[],
    )
    return enterprise_answer.to_dict()


def _execution_strategy_for() -> dict:
    d = ExecutionStrategy(
        strategy_type=StrategyType.SQL_REQUIRED,
        requires_sql=True,
        requires_live_data=True,
        confidence=0,
    ).to_dict()
    d["implementation"] = _EXECUTION_STRATEGY_IMPLEMENTATION
    return d


# ── Public entry point ────────────────────────────────────────────────────


def build_ai_first_composer_response(
    result: AiFirstOrchestrationResult,
    *,
    session_id: str,
    request_id: str,
    execution_time: float,
    conversation_state: Optional[dict] = None,
) -> dict:
    """
    Adapts one AiFirstOrchestrationResult, plus the minimal request/session
    context POST /v1/composer/ask has in hand, into that route's AI-
    Workspace-facing response contract. Pure and deterministic -- see
    module docstring.
    """
    return {
        "status": "success",
        "request_id": request_id,
        "session_id": session_id,
        "business_answer": _business_answer_for(result),
        "resolved_intent": dict(FIXED_RESOLVED_INTENT),
        "services_selected": list(_SERVICES_SELECTED),
        "evidence_summary": _evidence_summary_for(result),
        "evidence_package": None,
        "governance_state": None,
        "confidence": 0.0,
        "execution_time": execution_time,
        "warnings": list(result.warnings),
        "errors": [result.error] if result.error else [],
        "enterprise_answer": _enterprise_answer_for(result),
        "execution_strategy": _execution_strategy_for(),
        "agent_status": _AGENT_STATUS_BY_STATUS[result.status],
        "pipeline_status": _PIPELINE_STATUS_BY_STATUS[result.status],
        "agent_trace": _agent_trace_for(result),
        "conversation_state": conversation_state,
        "perf_trace": _perf_trace_for(result),
        # Additive, presentation-only: "HIT"/"MISS" + an honest explanatory note.
        "plan_cache_status": _plan_cache_status_for(result),
        "plan_cache_note": _plan_cache_note_for(result),
        # Additive, presentation-only fields for the frontend's Technical
        # Details panel. Both None when this turn had no prior-turn context
        # to resolve against (a fresh question). Never a row value, raw PII
        # value, credential, or full prompt.
        "follow_up_resolution": {
            "resolution_type": result.follow_up_resolution_type,
            "resolved_standalone_question": result.resolved_standalone_question,
        },
    }
