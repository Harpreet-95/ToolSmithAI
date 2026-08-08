"""
Agent response adapters — Enterprise Milestone M-30.

Narrow, shared translation of core.orchestrator.agent.AgentState into each
production route's existing response contract. Used by both
api/v1/composer.py (POST /v1/composer/ask) and api/v1/routes.py
(POST /v1/sources/{id}/execute-query) so neither route reimplements its own
AgentState-to-response mapping — one adapter, two callers.

No business logic lives here: every field below is read straight off
AgentState/EnterpriseAnswer, never recomputed or re-validated. Trace entries
are passed through as-is — core.orchestrator.agent._step() already scrubs
them to concise, UI-safe operational evidence (counts, table/column names,
status/reason codes) with no chain-of-thought, raw row samples, or secrets;
this module does not need to (and must not) filter them further, only shape
them into plain dicts for JSON.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from core.orchestrator.agent import AgentState, AgentStatus, ConversationContext

# The one execute-query outcome with a real historical precedent: before
# M-30, execute_query_route always called execute_generated_query() even for
# a plan the SQL planner had already refused, and that function's own safety
# gate (sql_plan.validation.valid is False) always produced a
# status="governance_block" result without ever opening a connection. A
# safely-refused agent outcome (sql_generation_refused / no_valid_join)
# reproduces that same observable execution.status for existing consumers.
# clarification_required and step_limit_reached (hit before any execution)
# have no such precedent — execute-query never asked for clarification
# before the agent existed — so they get an honestly-named new status
# rather than a borrowed, inaccurate label.
_LEGACY_EXECUTION_STATUS_BY_AGENT_STATUS: dict[AgentStatus, str] = {
    AgentStatus.SAFELY_REFUSED: "governance_block",
    AgentStatus.CLARIFICATION_REQUIRED: "not_executed",
    AgentStatus.STEP_LIMIT_REACHED: "not_executed",
}


def agent_trace_summary(state: AgentState) -> list[dict]:
    """Plain-dict form of state.trace — already concise, already free of
    chain-of-thought/raw data/secrets by construction (see TraceStep's own
    docstring in core.orchestrator.agent)."""
    return [
        {
            "step": s.step, "tool": s.tool, "status": s.status,
            "input_summary": s.input_summary, "output_summary": s.output_summary,
            "reason_code": s.reason_code, "duration_ms": s.duration_ms,
        }
        for s in state.trace
    ]


def _synthesized_execution_result(state: AgentState) -> dict:
    """Built only when the agent never reached core.live.query_engine's
    execute() at all (state.execution_result is None) — same field shape as
    a real QueryResult.to_dict(), so existing consumers reading
    data["execution"]["status"]/["error"] never see a missing key."""
    status = _LEGACY_EXECUTION_STATUS_BY_AGENT_STATUS.get(state.status, "not_executed")
    return {
        "execution_id": None, "status": status, "source_id": state.source_id,
        "executed_at": None, "duration_ms": None,
        "columns": [], "rows": [], "row_count": 0, "truncated": False,
        "row_limit_applied": 0, "warnings": [], "error": state.stop_reason,
    }


def build_execute_query_response(state: AgentState) -> tuple[int, dict]:
    """
    Adapts AgentState into POST /sources/{id}/execute-query's existing
    response contract — {"status": "success", "data": {query_plan, sql_plan,
    sql_generation, execution, [metadata_preparation]}} — plus additive
    agent_status/stop_reason/warnings/trace fields new consumers can read.
    Returns (http_status_code, body).

    Ownership failure (stop_code == "unowned_source") keeps the route's
    original 404 contract — the one case that was never wrapped in
    {"status": "success", ...}. Every other terminal state — including
    genuine ambiguity, which this route could never previously reach since
    it registered no clarification hook — stays a 200 with the original
    data.* keys intact, per "do not convert ... into generic 500 errors."
    """
    from core.errors.error_response import build_error_response

    if state.stop_code == "unowned_source":
        return 404, build_error_response("Data source not found.")

    data: dict[str, Any] = {
        "query_plan": state.query_plan,
        "sql_plan": state.sql_plan,
        "sql_generation": state.sql_generation,
        "execution": state.execution_result or _synthesized_execution_result(state),
    }
    if state.preparation_trace:
        data["metadata_preparation"] = state.preparation_trace
    if state.clarification:
        data["clarification"] = state.clarification

    body = {
        "status": "success",
        "agent_status": state.status.value if state.status else None,
        "stop_reason": state.stop_reason,
        "data": data,
        "warnings": state.warnings,
        "trace": agent_trace_summary(state),
    }
    return 200, body


def build_conversation_state(
    state: AgentState, *, conversation_id: str, source_id: int, user_id: str, turn_number: int,
) -> Optional[dict]:
    """
    Milestone M-31 — the next turn's round-trippable ConversationContext, as
    a plain dict (api/v1/composer.py's ComposerRequest.conversation_state
    contract). Built only when the agent actually produced a resolved
    query_plan + business_plan (state.query_plan/state.answer_evidence_data)
    — the same precondition a follow-up merge itself requires — so a
    clarification/refusal/error turn simply has nothing to hand forward and
    this returns None, the correct safe-fallback input for the next call.

    clarification_selection/cancel_clarification are never carried forward
    here — those are one-turn resume/cancel instructions, not state — which
    is what bounds clarification resume to at most one cycle: the next
    turn's context never contains a nested prior clarification to resume a
    second time.
    """
    business_plan = (state.answer_evidence_data or {}).get("business_plan") if state.query_plan else None
    if not state.query_plan or not business_plan:
        return None

    dimension_labels = business_plan.get("dimension_labels") or {}
    context = ConversationContext(
        conversation_id=conversation_id, source_id=source_id, user_id=user_id,
        turn_number=turn_number, created_at=datetime.now(timezone.utc).isoformat(),
        previous_question=state.original_question,
        previous_effective_question=state.effective_question,
        previous_query_plan=state.query_plan,
        previous_business_plan=business_plan,
        selected_tables=tuple(state.selected_tables),
        metric=business_plan.get("measure_label") or business_plan.get("entity_label"),
        dimensions=tuple(dimension_labels.values()),
        filters=tuple(business_plan.get("where") or ()),
        time_range=business_plan.get("date_context"),
    )
    return context.to_dict()


def build_composer_agent_response(
    *, state: AgentState, resolved_intent, session_id: str, request_id: str, execution_time: float,
    conversation_state: Optional[dict] = None,
) -> dict:
    """
    Adapts AgentState into POST /composer/ask's existing response contract
    for a business-data SQL question. Reuses api/v1/composer.py's own
    existing _generate_answer()/_extract_warnings()/_determine_status()/
    _extract_governance_state()/_serialize_package() helpers — unchanged,
    over an equivalent single-item EvidencePackage
    (core.orchestrator.agent.build_live_query_package) — so business_answer/
    warnings/status/governance_state/evidence_package are derived exactly as
    they always were for a live_query evidence item, not recomputed a
    second, different way.

    enterprise_answer is state.answer itself (already built by the agent,
    via the same AnswerPlanner facade composer.py's own Enterprise Answer
    Generation Layer already called) — not recomputed via a second
    AnswerPlanner.build() call. agent_status/agent_trace are additive new
    fields; every key the pre-M-30 response always returned is still present.

    conversation_state (Milestone M-31), if given, is passed through
    verbatim as an additive field — build_conversation_state()'s own output —
    for the caller to round-trip on the next request's
    ComposerRequest.conversation_state.
    """
    from api.v1.composer import (
        _determine_status, _extract_governance_state, _extract_warnings,
        _generate_answer, _serialize_package,
    )
    from core.execution.execution_strategy import ExecutionStrategy, StrategyType
    from core.orchestrator.agent import build_live_query_package

    package = build_live_query_package(
        state.effective_question, state.answer_evidence_data, source_id=state.source_id,
        intent_confidence=getattr(resolved_intent, "confidence", None),
    )
    business_answer = _generate_answer(package)
    status = _determine_status(package)
    governance_state = _extract_governance_state(package)
    warnings = _extract_warnings(package)

    confidence = resolved_intent.confidence if resolved_intent else 0.0
    execution_strategy = ExecutionStrategy(
        strategy_type=StrategyType.SQL_REQUIRED, requires_sql=True, requires_live_data=True,
        confidence=int(round(confidence * 100)),
    )

    return {
        "status": status,
        "request_id": request_id,
        "session_id": session_id,
        "business_answer": business_answer,
        "resolved_intent": {
            "intent_type": resolved_intent.intent_type.value if resolved_intent else "sql_request",
            "confidence": confidence,
            "required_capabilities": (
                [c.value for c in resolved_intent.required_capabilities] if resolved_intent else []
            ),
            "secondary_intents": (
                [i.value for i in resolved_intent.secondary_intents] if resolved_intent else []
            ),
            "keywords_matched": resolved_intent.keywords_matched if resolved_intent else [],
        },
        "services_selected": ["live_query"],
        "evidence_summary": {
            "total_items": package.total_evidence_items,
            "services_attempted": package.services_attempted,
            "services_succeeded": package.services_succeeded,
            "has_errors": bool(package.errors),
        },
        "evidence_package": _serialize_package(package),
        "governance_state": governance_state,
        "confidence": confidence,
        "execution_time": execution_time,
        "warnings": warnings,
        "errors": package.errors,
        "enterprise_answer": state.answer.to_dict() if state.answer is not None else None,
        "execution_strategy": execution_strategy.to_dict(),
        "agent_status": state.status.value if state.status else None,
        "agent_trace": agent_trace_summary(state),
        "conversation_state": conversation_state,
        # Day 4, Capability 6 — orchestration-layer stage timing breakdown
        # (core.perf.stage_timer). Additive/observability only.
        "perf_trace": state.perf_trace,
    }
