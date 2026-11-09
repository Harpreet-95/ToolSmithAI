"""
Fast-path prior-turn context — Phase 7, Step 6F, Part 2.

Pure, no-I/O conversions between api.v1.sql_pipeline_composer_state.
NewPipelineConversationState (the client-round-tripped envelope) and the
bounded, structural context core.sqlservice.fast_path_generation_service's
single combined call needs to genuinely resolve a follow-up — never a
second pipeline, never a parallel state store.

FastPathPriorTurnContext carries ONLY what Part 2 of the Step 6F
requirement names: the previous user question, the previous turn's own
resolved standalone question, which objects/columns/filters/grouping it
used, and (when applicable) the clarification question it asked. It never
carries returned row values, raw PII values, credentials, secrets,
vectors, or a complete prompt — none of those are present on
NewPipelineConversationState in the first place (see that module's own
docstring), so none can leak through here either.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from api.v1.sql_pipeline_composer_state import (
    KIND_CLARIFICATION_PENDING,
    NewPipelineConversationState,
)

__all__ = [
    "FastPathPriorTurnContext",
    "build_prior_turn_context",
    "next_turn_context_from_candidate",
    "render_prior_turn_context_text",
]


@dataclass(frozen=True)
class FastPathPriorTurnContext:
    """The bounded previous-turn context handed to the fast path's one
    combined call. `source_id`/`user_id`/`conversation_id` are never used
    to derive authorization here (identical rule to NewPipelineConversationState
    itself) — they exist only so a caller can log/display which turn this
    context came from; the REAL isolation check (source/user/session match)
    already happened before this object is ever built (see
    api/v1/composer.py's existing conversation_state validation, unchanged
    by this module)."""

    previous_user_question: str
    resolved_standalone_question: str
    selected_object_fqns: tuple
    selected_output_columns: tuple
    filters: tuple
    groupings: tuple
    date_constraints: tuple
    operation: Optional[str]
    requested_attributes: tuple
    pending_clarification_question: Optional[str]
    is_clarification_resume: bool
    schema_fingerprint: Optional[str]
    business_knowledge_package_id: Optional[int]
    business_knowledge_package_version: Optional[int]
    view_profile_state: tuple
    secret_policy_version: Optional[int]
    source_id: int
    conversation_id: Optional[str] = None
    user_id: Optional[str] = None


def build_prior_turn_context(
    state: NewPipelineConversationState, *, conversation_id: Optional[str] = None, user_id: Optional[str] = None,
) -> FastPathPriorTurnContext:
    """Builds the bounded context from an already-validated (current,
    non-expired, source-matched) NewPipelineConversationState. Caller is
    responsible for having already validated the state (composer.py's
    existing parse_new_pipeline_state/is_new_pipeline_state_current checks,
    unchanged) — this function does no further trust decision, it only
    reshapes already-trusted fields.

    `is_clarification_resume` is True iff the prior turn's own `kind` was
    `clarification_pending` (i.e. the round-tripped question is the user's
    ANSWER to `pending_clarification_question`, not a plain follow-up)."""
    return FastPathPriorTurnContext(
        previous_user_question=state.last_question,
        resolved_standalone_question=state.last_question,
        selected_object_fqns=state.selected_object_fqns,
        selected_output_columns=state.selected_output_columns,
        filters=state.filters,
        groupings=state.groupings,
        date_constraints=state.date_constraints,
        operation=state.operation,
        requested_attributes=state.requested_attributes,
        pending_clarification_question=state.pending_clarification_question,
        is_clarification_resume=(state.kind == KIND_CLARIFICATION_PENDING),
        schema_fingerprint=state.schema_fingerprint,
        business_knowledge_package_id=state.business_knowledge_package_id,
        business_knowledge_package_version=state.business_knowledge_package_version,
        view_profile_state=state.view_profile_state,
        secret_policy_version=state.secret_policy_version,
        source_id=state.source_id,
        conversation_id=conversation_id,
        user_id=user_id,
    )


def next_turn_context_from_candidate(candidate) -> dict:
    """Builds the plain dict of structural fields to persist as the NEXT
    turn's own prior-turn context, from a core.sqlservice.
    fast_path_generation_service.FastPathGenerationCandidate that was
    request_kind == DATA_QUESTION (the only case with a real SQL plan to
    describe). `candidate.object_reasons` is flattened/deduped into one
    bounded column-name list — never a row value, never a full per-object
    breakdown — since the follow-up contract only needs "what columns were
    on screen last time", not which object each one came from.

    Returns a dict with keys matching build_answered_state's own kwargs
    (selected_object_fqns/selected_output_columns/filters/groupings/
    date_constraints/operation/requested_attributes) so a caller can
    `build_answered_state(..., **next_turn_context_from_candidate(candidate))`."""
    seen: set[str] = set()
    columns: list[str] = []
    for reason in candidate.object_reasons:
        for column in reason.supporting_columns:
            if column not in seen:
                seen.add(column)
                columns.append(column)
    return {
        "selected_object_fqns": tuple(candidate.selected_object_fqns),
        "selected_output_columns": tuple(columns),
        "filters": tuple(candidate.intent.filters),
        "groupings": tuple(candidate.intent.groupings),
        "date_constraints": tuple(candidate.intent.date_constraints),
        "operation": candidate.intent.operation,
        "requested_attributes": tuple(candidate.intent.requested_attributes),
    }


def render_prior_turn_context_text(prior_turn: FastPathPriorTurnContext) -> str:
    """Deterministic, bounded plain-text rendering of `prior_turn` for the
    fast-path prompt's own CONVERSATION_CONTEXT section (see
    core.sqlservice.fast_path_generation_service._build_prompts). Pure
    string formatting, no I/O, no model call — every field is already
    bounded (see FastPathPriorTurnContext's own source, validated by
    api.v1.sql_pipeline_composer_state.parse_new_pipeline_state before this
    is ever called)."""
    lines = [f"PREVIOUS_RESOLVED_QUESTION: {prior_turn.resolved_standalone_question}"]
    if prior_turn.selected_object_fqns:
        lines.append(f"PREVIOUS_SELECTED_OBJECTS: {', '.join(prior_turn.selected_object_fqns)}")
    if prior_turn.selected_output_columns:
        lines.append(f"PREVIOUS_OUTPUT_COLUMNS: {', '.join(prior_turn.selected_output_columns)}")
    if prior_turn.operation:
        lines.append(f"PREVIOUS_OPERATION: {prior_turn.operation}")
    if prior_turn.filters:
        lines.append(f"PREVIOUS_FILTERS: {', '.join(prior_turn.filters)}")
    if prior_turn.groupings:
        lines.append(f"PREVIOUS_GROUPINGS: {', '.join(prior_turn.groupings)}")
    if prior_turn.date_constraints:
        lines.append(f"PREVIOUS_DATE_CONSTRAINTS: {', '.join(prior_turn.date_constraints)}")
    if prior_turn.requested_attributes:
        lines.append(f"PREVIOUS_REQUESTED_ATTRIBUTES: {', '.join(prior_turn.requested_attributes)}")
    if prior_turn.is_clarification_resume and prior_turn.pending_clarification_question:
        lines.append(f"PENDING_CLARIFICATION_QUESTION_ASKED: {prior_turn.pending_clarification_question}")
        lines.append("THIS_MESSAGE_IS: the user's answer to that clarification question, not a fresh question.")
    return "\n".join(lines)
