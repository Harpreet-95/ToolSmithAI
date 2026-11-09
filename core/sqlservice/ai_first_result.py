"""
Shared result model and status vocabulary for the AI-first fast path.

Phase 8A: the older multi-call AI-first pipeline (frontier -> separate
schema-selection call -> separate SQL-generation call -> separate SQL-review
call -> correction) was retired; its orchestration entry point and its
reviewer stage no longer exist. What remains here is the ONE result shape
and the STATUS_*/ERROR_CATEGORY_* vocabulary that
core.sqlservice.ai_first_fast_path_service produces and that
api/v1/ai_first_composer_adapter.py / api/v1/query_response_adapter.py
translate -- plus the single result-assembly helper
(build_ai_first_orchestration_result).

The production flow lives in core.sqlservice.ai_first_fast_path_service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.sqlservice.answer_generation_providers import AnswerGenerationProvider
from core.sqlservice.answer_generation_service import AnswerGenerationResult
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.retrieval_store import StoredRetrievalBuild
from core.sqlservice.schema_context_retrieval import SchemaContextResult
from core.sqlservice.schema_frontier import SchemaFrontierResult
from core.sqlservice.schema_relationship_completion import RelationshipCompletionResult
from core.sqlservice.schema_selection_providers import SchemaSelectionProvider
from core.sqlservice.schema_selection_service import SchemaSelectionResult
from core.sqlservice.sql_execution import SqlExecutionResult
from core.sqlservice.sql_generation_providers import TextToSqlProvider
from core.sqlservice.sql_generation_service import SqlGenerationCandidate, SqlGenerationResult
from core.sqlservice.sql_validation import SqlValidationResult

__all__ = [
    "STATUS_OK",
    "STATUS_NOT_PREPARED",
    "STATUS_RETRIEVAL_FAILED",
    "STATUS_SELECTION_FAILED",
    "STATUS_RELATIONSHIP_COMPLETION_FAILED",
    "STATUS_CONTEXT_BUILD_FAILED",
    "STATUS_GENERATION_FAILED",
    "STATUS_CLARIFICATION_REQUIRED",
    "STATUS_NON_DATA",
    "STATUS_UNSUPPORTED",
    "STATUS_VALIDATION_FAILED",
    "STATUS_EXECUTION_FAILED",
    "STATUS_ANSWER_FAILED",
    "STATUS_BUDGET_EXCEEDED",
    "ERROR_CATEGORY_CONFIGURATION",
    "ERROR_CATEGORY_PROVIDER",
    "ERROR_CATEGORY_INVALID_RESPONSE",
    "AiFirstStageDurations",
    "AiFirstOrchestrationResult",
    "build_ai_first_orchestration_result",
]

STATUS_OK = "OK"
STATUS_NOT_PREPARED = "NOT_PREPARED"
STATUS_RETRIEVAL_FAILED = "RETRIEVAL_FAILED"  # candidate-frontier build failed or returned zero candidates
STATUS_SELECTION_FAILED = "SELECTION_FAILED"  # combined-call exception, malformed output, or a non-OK result
STATUS_RELATIONSHIP_COMPLETION_FAILED = "RELATIONSHIP_COMPLETION_FAILED"  # infra failure, not a disconnected selection
STATUS_CONTEXT_BUILD_FAILED = "CONTEXT_BUILD_FAILED"  # infra failure, not an oversized/incomplete context
STATUS_GENERATION_FAILED = "GENERATION_FAILED"
STATUS_CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"  # combined call, relationship completion, context build, or ambiguity guard asked for clarification
STATUS_NON_DATA = "NON_DATA"  # classified as not a data question at all (e.g. a greeting)
STATUS_UNSUPPORTED = "UNSUPPORTED"  # classified as a write/destructive/administrative request that is never executed
STATUS_VALIDATION_FAILED = "VALIDATION_FAILED"
STATUS_EXECUTION_FAILED = "EXECUTION_FAILED"
STATUS_ANSWER_FAILED = "ANSWER_FAILED"
STATUS_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"

ERROR_CATEGORY_CONFIGURATION = "configuration"
ERROR_CATEGORY_PROVIDER = "provider"
ERROR_CATEGORY_INVALID_RESPONSE = "invalid_response"


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class AiFirstStageDurations:
    """Milliseconds spent in each stage, measured with the SAME injected
    clock as everything else in the fast path. A stage that never ran is
    None, never 0. total_ms is always measured."""

    frontier_ms: Optional[int]
    selection_ms: Optional[int]
    relationship_ms: Optional[int]
    context_ms: Optional[int]
    ambiguity_guard_ms: Optional[int]
    generation_ms: Optional[int]
    validation_ms: Optional[int]
    execution_ms: Optional[int]
    answer_ms: Optional[int]
    total_ms: int


@dataclass(frozen=True)
class AiFirstOrchestrationResult:
    """
    Result of one fast-path question. Every collection field is a tuple --
    never a list or dict -- so the whole result is immutable.

    status: one of the STATUS_* constants above.
    frontier_size / selector_status / selected_object_fqns /
        connector_object_fqns / selector_provider_id / selector_model:
        orchestration observability. `frontier_size` is 0 until the frontier
        stage actually returns; `selected_object_fqns`/`connector_object_fqns`
        are empty until each respective stage succeeds -- populated even on a
        later-stage failure so a caller can always see what was selected/
        connected up to the point of failure.
    sql: validation_result.sql once validation has succeeded, otherwise the
        candidate's own sql, otherwise None.
    warnings: every warning from every stage that actually ran, in stage
        order.
    error / error_category: populated on every non-OK, non-
        CLARIFICATION_REQUIRED status; provider-layer text is never
        forwarded verbatim (see the fast path's _stage_exception_outcome).
    cache_hit: True/False -- whether THIS result's one combined-generation
        stage was served from a validated plan cache (True) or made a real
        embedding + model call (False). Never changes what ran --
        deterministic validation and live SQL execution happen
        unconditionally either way.
    resolved_standalone_question / follow_up_resolution_type /
        next_turn_context: follow-up observability. None for a fresh
        question; for a follow-up/clarification-answer turn, the ONE
        standalone question the combined call resolved the message to and
        which bounded resolution_type it judged the turn to be.
        api/v1/ai_first_composer_execution.py uses
        resolved_standalone_question (never the raw message) as the NEXT
        turn's own last_question, and next_turn_context to populate the
        next turn's structural follow-up fields -- both are None whenever
        status is not STATUS_OK.
    """

    status: str
    source_id: int
    build_id: Optional[int]
    snapshot_id: Optional[int]
    schema_fingerprint: Optional[str]
    answer: str
    insights: tuple
    caveats: tuple
    follow_up_questions: tuple
    clarification_question: Optional[str]
    interpretation: Optional[str]
    sql: Optional[str]
    canonical_referenced_object_fqns: tuple
    columns: tuple
    rows: tuple
    row_count: int
    truncated: bool
    truncation_reason: Optional[str]
    retrieval_documents: tuple
    embedding_provider_id: str
    embedding_model: str
    frontier_size: int
    selector_status: Optional[str]
    selected_object_fqns: tuple
    connector_object_fqns: tuple
    selector_provider_id: str
    selector_model: str
    sql_provider_id: str
    sql_model: str
    answer_provider_id: str
    answer_model: str
    warnings: tuple
    error: Optional[str]
    error_category: Optional[str]
    durations: AiFirstStageDurations
    cache_hit: Optional[bool] = None
    resolved_standalone_question: Optional[str] = None
    follow_up_resolution_type: Optional[str] = None
    next_turn_context: Optional[dict] = None


# ── Result assembly ──────────────────────────────────────────────────────


def build_ai_first_orchestration_result(
    status: str,
    *,
    source_id: int,
    build_row: Optional[StoredRetrievalBuild],
    embedding_provider: EmbeddingProvider,
    schema_selection_provider: SchemaSelectionProvider,
    text_to_sql_provider: TextToSqlProvider,
    answer_provider: AnswerGenerationProvider,
    candidate_frontier: Optional[SchemaFrontierResult] = None,
    selection_result: Optional[SchemaSelectionResult] = None,
    relationship_result: Optional[RelationshipCompletionResult] = None,
    schema_context: Optional[SchemaContextResult] = None,
    generation_result: Optional[SqlGenerationResult] = None,
    validation_result: Optional[SqlValidationResult] = None,
    execution_result: Optional[SqlExecutionResult] = None,
    answer_result: Optional[AnswerGenerationResult] = None,
    clarification_question: Optional[str] = None,
    answer_override: Optional[str] = None,
    deterministic_insights: tuple = (),
    error: Optional[str] = None,
    error_category: Optional[str] = None,
    cache_hit: Optional[bool] = None,
    resolved_standalone_question: Optional[str] = None,
    follow_up_resolution_type: Optional[str] = None,
    next_turn_context: Optional[dict] = None,
    durations: dict,
    total_ms: int,
) -> AiFirstOrchestrationResult:
    warnings: list = []
    if candidate_frontier is not None:
        warnings.extend(candidate_frontier.warnings)
    if selection_result is not None:
        warnings.extend(selection_result.warnings)
    if relationship_result is not None:
        warnings.extend(relationship_result.warnings)
    if schema_context is not None:
        warnings.extend(schema_context.warnings)
    if generation_result is not None:
        warnings.extend(generation_result.warnings)
    if execution_result is not None:
        warnings.extend(execution_result.warnings)
    if answer_result is not None:
        warnings.extend(answer_result.warnings)

    candidate: Optional[SqlGenerationCandidate] = (
        generation_result.candidate if generation_result is not None else None
    )
    if validation_result is not None:
        sql = validation_result.sql
    elif candidate is not None and candidate.needs_clarification:
        sql = candidate.sql  # always "" -- a clarification candidate
    else:
        sql = None  # generation failed / never ran, or validation did not succeed: never report unvalidated SQL

    selection_candidate = selection_result.candidate if selection_result is not None else None

    return AiFirstOrchestrationResult(
        status=status,
        source_id=source_id,
        build_id=build_row.id if build_row is not None else None,
        snapshot_id=build_row.snapshot_id if build_row is not None else None,
        schema_fingerprint=build_row.schema_fingerprint if build_row is not None else None,
        answer=(
            answer_result.answer if answer_result is not None
            else (answer_override if answer_override is not None else "")
        ),
        insights=(
            answer_result.insights if answer_result is not None and answer_result.insights
            else deterministic_insights
        ),
        caveats=answer_result.caveats if answer_result is not None else (),
        follow_up_questions=answer_result.follow_up_questions if answer_result is not None else (),
        clarification_question=clarification_question,
        interpretation=candidate.interpretation if candidate is not None else None,
        sql=sql,
        canonical_referenced_object_fqns=(
            validation_result.canonical_referenced_object_fqns if validation_result is not None else ()
        ),
        columns=execution_result.columns if execution_result is not None else (),
        rows=execution_result.rows if execution_result is not None else (),
        row_count=execution_result.row_count if execution_result is not None else 0,
        truncated=execution_result.truncated if execution_result is not None else False,
        truncation_reason=execution_result.truncation_reason if execution_result is not None else None,
        retrieval_documents=schema_context.documents if schema_context is not None else (),
        embedding_provider_id=embedding_provider.provider_id,
        embedding_model=embedding_provider.model,
        frontier_size=len(candidate_frontier.candidates) if candidate_frontier is not None else 0,
        selector_status=selection_candidate.response_status if selection_candidate is not None else None,
        selected_object_fqns=(
            selection_candidate.selected_object_fqns if selection_candidate is not None else ()
        ),
        connector_object_fqns=(
            relationship_result.connector_object_fqns if relationship_result is not None else ()
        ),
        selector_provider_id=schema_selection_provider.provider_id,
        selector_model=selection_result.model if selection_result is not None else schema_selection_provider.model,
        sql_provider_id=text_to_sql_provider.provider_id,
        sql_model=generation_result.model if generation_result is not None else text_to_sql_provider.model,
        answer_provider_id=answer_provider.provider_id,
        answer_model=answer_result.model if answer_result is not None else answer_provider.model,
        warnings=tuple(warnings),
        error=error,
        error_category=error_category,
        cache_hit=cache_hit,
        resolved_standalone_question=resolved_standalone_question,
        follow_up_resolution_type=follow_up_resolution_type,
        next_turn_context=next_turn_context,
        durations=AiFirstStageDurations(
            frontier_ms=durations.get("frontier_ms"),
            selection_ms=durations.get("selection_ms"),
            relationship_ms=durations.get("relationship_ms"),
            context_ms=durations.get("context_ms"),
            ambiguity_guard_ms=durations.get("ambiguity_guard_ms"),
            generation_ms=durations.get("generation_ms"),
            validation_ms=durations.get("validation_ms"),
            execution_ms=durations.get("execution_ms"),
            answer_ms=durations.get("answer_ms"),
            total_ms=total_ms,
        ),
    )
