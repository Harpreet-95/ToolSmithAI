"""
AI-first fast-path Composer execution helper -- Phase 8A (architecture
consolidation).

The ONE question-answering route behind POST /v1/composer/ask for any
request that names a connected SQL datasource. `answer_with_fast_path` is
the single entry point composer.py calls; everything below it is a small,
directly-testable helper:

    follow-up / clarification-answer / fresh-question dispatch
      -> core.sqlservice.ai_first_fast_path_service.
         answer_question_ai_first_fast_path()   (one combined GPT call, or a
         validated plan from the durable/in-memory plan cache; SQL is
         deterministically validated and executed LIVE on every request,
         cache hit or not)
      -> api/v1/ai_first_composer_adapter.build_ai_first_composer_response()

There is NO fallback into any other pipeline: a failure returns the
fast path's own typed status (or the sanitized unexpected-failure
envelope below), never a silent retry elsewhere. Request-kind judgment
(data question / clarification / greeting-or-non-data / destructive-or-
unsupported) and follow-up resolution happen inside the fast path's own
combined model call -- there is no keyword intent classifier on this path.

Conversation state reuses api/v1/sql_pipeline_composer_state.py's
validated, pipeline-agnostic state shape (STATE_TYPE/build_answered_state/
build_clarification_state). That module is untrusted-input parsing only --
it never executes anything.

No database access beyond one local SQLite read (is_new_pipeline_state_
current), no route-level concerns (JWT) here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from core.answering.models import AnswerType, EnterpriseAnswer
from core.execution.execution_strategy import ExecutionStrategy, StrategyType
from core.sqlservice.ai_first_fast_path_service import answer_question_ai_first_fast_path
from core.sqlservice.ai_first_result import (
    STATUS_CLARIFICATION_REQUIRED,
    STATUS_OK,
)
from core.sqlservice.answer_generation_providers import OpenAIAnswerGenerationProvider
from core.sqlservice.embedding_providers import OpenAIEmbeddingProvider
from core.sqlservice.fast_path_generation_providers import OpenAIFastPathGenerationProvider
from core.sqlservice.fast_path_prior_turn import FastPathPriorTurnContext, build_prior_turn_context

from api.v1.ai_first_composer_adapter import FIXED_RESOLVED_INTENT, build_ai_first_composer_response
from api.v1.sql_pipeline_composer_state import (
    KIND_ANSWERED,
    KIND_CLARIFICATION_PENDING,
    MAX_CLARIFICATION_TURNS,
    NewPipelineConversationState,
    build_answered_state,
    build_clarification_state,
    exceeds_question_length_limit,
    has_new_pipeline_state_type,
    parse_new_pipeline_state,
)

logger = logging.getLogger(__name__)

__all__ = [
    "answer_with_fast_path",
    "is_fast_path_enabled_for_source",
    "run_ai_first_fast_path_composer_ask",
    "run_ai_first_fast_path_composer_continue",
    "build_ai_first_cancellation_response",
    "build_ai_first_state_reset_response",
    "build_source_not_enabled_response",
    "is_new_pipeline_state_current",
]

_EXECUTION_STRATEGY_IMPLEMENTATION = "sqlservice_ai_first"
_SERVICES_SELECTED = ["live_query"]

# A genuinely unexpected failure only -- the fast path already turns every
# anticipated failure into a normal, already-sanitized result.
_UNEXPECTED_ERROR_MESSAGE = (
    "The AI-first query pipeline is temporarily unavailable. Please try again "
    "or contact your administrator."
)

_CLARIFICATION_LIMIT_MESSAGE = (
    "This question still needs clarification after two attempts. "
    "Please restate your complete question in a single message, including "
    "any specifics (such as which table, column, or time period you mean)."
)

_CANCELLATION_MESSAGE = "Clarification was cancelled. No query was executed."

_QUESTION_TOO_LONG_MESSAGE = (
    "Your question, combined with the clarification details provided so "
    "far, is too long to process. Please restate your complete question "
    "in a single message."
)

_STATE_RESET_MESSAGE = (
    "Your previous conversation context could not be validated and has "
    "been reset. Please restate your complete question in a single "
    "message."
)

_SOURCE_NOT_ENABLED_MESSAGE = (
    "Question answering is not enabled for this data source. "
    "Ask your administrator to enable it, then try again. No query was executed."
)


def is_fast_path_enabled_for_source(source_id: Optional[int]) -> bool:
    """The single enablement gate: the global fast-path flag AND this exact
    source id in the operator's allow-list (core.config, read at call time
    so a test or operator override takes effect immediately)."""
    from core import config

    return bool(
        config.AI_FIRST_SQL_FAST_PATH_ENABLED
        and source_id is not None
        and source_id in config.AI_FIRST_SQL_FAST_PATH_SOURCE_IDS
    )


def is_new_pipeline_state_current(state: NewPipelineConversationState, *, user_id: str) -> bool:
    """
    A parsed-and-otherwise-valid conversation state is NOT trusted for
    resume/follow-up unless its stored build_id matches the CURRENT latest
    READY retrieval build for (source_id, user_id) -- if the source's schema
    has been re-scanned since the state was captured, the state is treated
    exactly like any other invalid state. One local SQLite read, no live
    source connection.
    """
    from core.sqlservice.retrieval_store import get_latest_ready_retrieval_build

    if state.build_id is None:
        return False
    current_build = get_latest_ready_retrieval_build(state.source_id, user_id)
    return current_build is not None and current_build.id == state.build_id


def _base_envelope(
    *, session_id: str, request_id: str, execution_time: float,
    agent_status: str, pipeline_status: str, answer: str, limitations: list, errors: list,
    conversation_state: Optional[dict],
) -> dict:
    """Shared envelope for the non-result responses this module produces
    (unexpected failure, clarification-limit-reached, cancellation, state
    reset, question-too-long, source-not-enabled), built from the same real
    EnterpriseAnswer/ExecutionStrategy dataclasses the adapter uses so none
    can drift from its field set."""
    enterprise_answer = EnterpriseAnswer(
        answer=answer,
        summary="",
        answer_type=AnswerType.LIVE_QUERY,
        confidence=0,
        limitations=limitations,
    ).to_dict()
    enterprise_answer["insights"] = []

    execution_strategy = ExecutionStrategy(
        strategy_type=StrategyType.SQL_REQUIRED,
        requires_sql=True,
        requires_live_data=True,
        confidence=0,
    ).to_dict()
    execution_strategy["implementation"] = _EXECUTION_STRATEGY_IMPLEMENTATION

    return {
        "status": "success",
        "request_id": request_id,
        "session_id": session_id,
        "business_answer": {
            "answer": answer,
            "answer_type": "live_query",
            "source_summary": f"SQL pipeline status: {agent_status}.",
            "confidence": 0.0,
            "limitations": limitations,
            "next_suggested_action": "",
        },
        "resolved_intent": dict(FIXED_RESOLVED_INTENT),
        "services_selected": list(_SERVICES_SELECTED),
        "evidence_summary": {
            "total_items": 0, "services_attempted": 1 if errors else 0,
            "services_succeeded": 0, "has_errors": bool(errors),
        },
        "evidence_package": None,
        "governance_state": None,
        "confidence": 0.0,
        "execution_time": execution_time,
        "warnings": [],
        "errors": errors,
        "enterprise_answer": enterprise_answer,
        "execution_strategy": execution_strategy,
        "agent_status": agent_status,
        "pipeline_status": pipeline_status,
        "agent_trace": [],
        "conversation_state": conversation_state,
        "perf_trace": {
            "retrieval_ms": None, "selection_ms": None, "relationship_ms": None, "context_ms": None,
            "ambiguity_guard_ms": None, "generation_ms": None, "validation_ms": None,
            "execution_ms": None, "answer_ms": None, "total_ms": None,
        },
    }


def _unexpected_failure_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="execution_failed", pipeline_status="EXECUTION_FAILED",
        answer=_UNEXPECTED_ERROR_MESSAGE, limitations=[_UNEXPECTED_ERROR_MESSAGE],
        errors=[_UNEXPECTED_ERROR_MESSAGE], conversation_state=None,
    )


def _clarification_limit_reached_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="clarification_limit_reached",
        pipeline_status="CLARIFICATION_REQUIRED",
        answer=_CLARIFICATION_LIMIT_MESSAGE, limitations=[_CLARIFICATION_LIMIT_MESSAGE],
        errors=[], conversation_state=None,
    )


def build_ai_first_cancellation_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="safely_refused", pipeline_status="OK",
        answer=_CANCELLATION_MESSAGE, limitations=[], errors=[], conversation_state=None,
    )


def _question_too_long_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="question_too_long", pipeline_status="VALIDATION_FAILED",
        answer=_QUESTION_TOO_LONG_MESSAGE, limitations=[_QUESTION_TOO_LONG_MESSAGE],
        errors=[], conversation_state=None,
    )


def build_ai_first_state_reset_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="state_reset", pipeline_status="VALIDATION_FAILED",
        answer=_STATE_RESET_MESSAGE, limitations=[], errors=[], conversation_state=None,
    )


def build_source_not_enabled_response(*, session_id: str, request_id: str, execution_time: float) -> dict:
    return _base_envelope(
        session_id=session_id, request_id=request_id,
        execution_time=execution_time, agent_status="not_enabled", pipeline_status="NOT_ENABLED",
        answer=_SOURCE_NOT_ENABLED_MESSAGE, limitations=[_SOURCE_NOT_ENABLED_MESSAGE],
        errors=[], conversation_state=None,
    )


def _next_state_for_result(result, *, fallback_question: str) -> Optional[dict]:
    """
    `result.resolved_standalone_question` (set whenever the fast path's
    follow-up resolution actually ran) is ALWAYS preferred over
    `fallback_question` (the raw per-turn message) as the next turn's own
    `last_question` -- it is the complete, standalone restatement, never a
    pronoun-laden fragment like "Sort them by last name."

    `result.next_turn_context` (STATUS_OK only) carries forward the bounded
    structural fields (selected objects/columns/filters/grouping) a LATER
    follow-up resolves against.
    """
    last_question = getattr(result, "resolved_standalone_question", None) or fallback_question
    if result.status == STATUS_OK:
        next_turn_context = getattr(result, "next_turn_context", None) or {}
        return build_answered_state(
            source_id=result.source_id, build_id=result.build_id,
            schema_fingerprint=result.schema_fingerprint, last_question=last_question,
            follow_up_resolution_type=getattr(result, "follow_up_resolution_type", None),
            **next_turn_context,
        )
    if result.status == STATUS_CLARIFICATION_REQUIRED:
        return build_clarification_state(
            source_id=result.source_id, build_id=result.build_id,
            schema_fingerprint=result.schema_fingerprint, last_question=last_question,
            pending_clarification_question=result.clarification_question, clarification_turns=1,
        )
    return None


def _run_ai_first_fast_path_pipeline(
    question: str, source_id: int, user_id: str, *, prior_turn: Optional[FastPathPriorTurnContext] = None,
):
    embedding_provider = OpenAIEmbeddingProvider()
    fast_path_provider = OpenAIFastPathGenerationProvider()
    # The answer provider is called only for a genuinely wide multi-row
    # result (see core.sqlservice.fast_path_answer_formatter.is_simple_result);
    # simple results are formatted deterministically.
    answer_provider = OpenAIAnswerGenerationProvider()
    return answer_question_ai_first_fast_path(
        question, source_id, user_id, embedding_provider, fast_path_provider, answer_provider,
        prior_turn=prior_turn,
    )


def run_ai_first_fast_path_composer_ask(
    source_id: int,
    user_id: str,
    question: str,
    *,
    session_id: str,
    request_id: str,
    start_time: float,
) -> dict:
    """
    One call = one fresh question answered through the fast path, already
    adapted to POST /v1/composer/ask's response contract. Never raises:
    the log line carries only source_id/stage/exception type -- never
    str(exc)/repr(exc)/exc_info, which could carry an API key, connection
    string, or provider response body.
    """
    stage = "provider_construction"
    try:
        stage = "answer_question_ai_first_fast_path_call"
        result = _run_ai_first_fast_path_pipeline(question, source_id, user_id)
    except Exception as exc:
        execution_time = round(time.perf_counter() - start_time, 4)
        logger.error(
            "run_ai_first_fast_path_composer_ask: unexpected failure "
            "[source_id=%s] [stage=%s] [exception_type=%s]",
            source_id, stage, type(exc).__name__,
        )
        return _unexpected_failure_response(
            session_id=session_id, request_id=request_id, execution_time=execution_time,
        )

    execution_time = round(time.perf_counter() - start_time, 4)
    next_state = _next_state_for_result(result, fallback_question=question)
    return build_ai_first_composer_response(
        result,
        session_id=session_id,
        request_id=request_id,
        execution_time=execution_time,
        conversation_state=next_state,
    )


def run_ai_first_fast_path_composer_continue(
    source_id: int,
    user_id: str,
    message: str,
    *,
    prior_state: NewPipelineConversationState,
    is_clarification_resume: bool,
    session_id: str,
    request_id: str,
    start_time: float,
) -> dict:
    """
    Resumes either a pending clarification or a genuine follow-up with
    EXACTLY ONE answer_question_ai_first_fast_path() call.

    `message` is the user's RAW, as-typed latest message (e.g. "Only active
    classes.", or "SQL course fees" in answer to a clarification question).
    core.sqlservice.fast_path_prior_turn.build_prior_turn_context() reshapes
    `prior_state` (already validated -- current, non-expired, source/schema-
    matched -- before this is called) into the bounded context the fast
    path's single combined model call uses to resolve `message` into one
    standalone question, classify whether it is a continuation/
    clarification-answer/comparison/drill-down/change/unrelated question,
    and answer it -- all in that ONE call.
    """
    if exceeds_question_length_limit(message):
        execution_time = round(time.perf_counter() - start_time, 4)
        return _question_too_long_response(
            session_id=session_id, request_id=request_id, execution_time=execution_time,
        )

    prior_turn = build_prior_turn_context(prior_state, conversation_id=session_id, user_id=user_id)

    stage = "provider_construction"
    try:
        stage = "answer_question_ai_first_fast_path_call"
        result = _run_ai_first_fast_path_pipeline(message, source_id, user_id, prior_turn=prior_turn)
    except Exception as exc:
        execution_time = round(time.perf_counter() - start_time, 4)
        logger.error(
            "run_ai_first_fast_path_composer_continue: unexpected failure "
            "[source_id=%s] [stage=%s] [exception_type=%s]",
            source_id, stage, type(exc).__name__,
        )
        return _unexpected_failure_response(
            session_id=session_id, request_id=request_id, execution_time=execution_time,
        )

    execution_time = round(time.perf_counter() - start_time, 4)
    # The next clarification round's own last_question must be the MODEL's
    # resolved standalone restatement, never the bare raw answer text.
    resolved_fallback = getattr(result, "resolved_standalone_question", None) or message

    if is_clarification_resume and result.status == STATUS_CLARIFICATION_REQUIRED:
        next_turn = prior_state.clarification_turns + 1
        if next_turn > MAX_CLARIFICATION_TURNS:
            return _clarification_limit_reached_response(
                session_id=session_id, request_id=request_id, execution_time=execution_time,
            )
        next_state = build_clarification_state(
            source_id=result.source_id, build_id=result.build_id,
            schema_fingerprint=result.schema_fingerprint, last_question=resolved_fallback,
            pending_clarification_question=result.clarification_question, clarification_turns=next_turn,
        )
    else:
        next_state = _next_state_for_result(result, fallback_question=resolved_fallback)

    return build_ai_first_composer_response(
        result,
        session_id=session_id,
        request_id=request_id,
        execution_time=execution_time,
        conversation_state=next_state,
    )


def answer_with_fast_path(
    *,
    source_id: int,
    user_id: str,
    message: str,
    conversation_state: Optional[dict[str, Any]],
    clarification_selection: Any,
    cancel_clarification: bool,
    session_id: str,
    request_id: str,
    start_time: float,
) -> dict:
    """
    The single question-answering entry point for a connected SQL
    datasource. Dispatch (no keyword classification anywhere):

      1. source not enabled              -> typed `not_enabled` response
      2. conversation_state that claims to be pipeline state but fails
         validation/currency (malformed, expired, other source, stale
         build)                          -> deterministic state-reset response
      3. cancel_clarification            -> cancellation response, no SQL
      4. valid pending-clarification state, or valid answered state
                                         -> continue (the model resolves
                                            the follow-up / clarification answer)
      5. anything else                   -> fresh question

    A conversation_state with no pipeline state_type at all (e.g. an old
    client's stale dict) is ignored and the message is treated as a fresh
    question -- never reinterpreted as continuity it cannot validate.
    """
    if not is_fast_path_enabled_for_source(source_id):
        return build_source_not_enabled_response(
            session_id=session_id, request_id=request_id,
            execution_time=round(time.perf_counter() - start_time, 4),
        )

    prior_state: Optional[NewPipelineConversationState] = None
    if conversation_state is not None and has_new_pipeline_state_type(conversation_state):
        candidate = parse_new_pipeline_state(conversation_state, source_id=source_id)
        if candidate is None or not is_new_pipeline_state_current(candidate, user_id=user_id):
            return build_ai_first_state_reset_response(
                session_id=session_id, request_id=request_id,
                execution_time=round(time.perf_counter() - start_time, 4),
            )
        prior_state = candidate

    if cancel_clarification:
        return build_ai_first_cancellation_response(
            session_id=session_id, request_id=request_id,
            execution_time=round(time.perf_counter() - start_time, 4),
        )

    if prior_state is not None and not clarification_selection:
        if prior_state.kind == KIND_CLARIFICATION_PENDING:
            return run_ai_first_fast_path_composer_continue(
                source_id, user_id, message, prior_state=prior_state, is_clarification_resume=True,
                session_id=session_id, request_id=request_id, start_time=start_time,
            )
        if prior_state.kind == KIND_ANSWERED:
            return run_ai_first_fast_path_composer_continue(
                source_id, user_id, message, prior_state=prior_state, is_clarification_resume=False,
                session_id=session_id, request_id=request_id, start_time=start_time,
            )

    return run_ai_first_fast_path_composer_ask(
        source_id, user_id, message,
        session_id=session_id, request_id=request_id, start_time=start_time,
    )
