"""
Focused tests — Phase 7, Step 6F, Part 2: the bounded follow-up contract
(core/sqlservice/fast_path_prior_turn.py).

Pure/in-memory: no database, no provider, no network call.
"""
from __future__ import annotations

from datetime import datetime, timezone

from api.v1.sql_pipeline_composer_state import (
    KIND_ANSWERED,
    KIND_CLARIFICATION_PENDING,
    build_answered_state,
    build_clarification_state,
    parse_new_pipeline_state,
)
from core.sqlservice.fast_path_generation_service import (
    FastPathFollowUpResolution,
    FastPathGenerationCandidate,
    FastPathIntent,
    FastPathObjectReason,
    REQUEST_KIND_DATA_QUESTION,
    RESOLUTION_TYPE_CONTINUATION,
)
from core.sqlservice.fast_path_prior_turn import (
    build_prior_turn_context,
    next_turn_context_from_candidate,
    render_prior_turn_context_text,
)

SOURCE_ID = 7


def _candidate(**overrides) -> FastPathGenerationCandidate:
    base = dict(
        request_kind=REQUEST_KIND_DATA_QUESTION,
        intent=FastPathIntent(
            operation="LIST", entities=("widget",), measures=(), filters=("active = 1",),
            date_constraints=(), groupings=(), requested_attributes=("name",),
        ),
        selected_object_fqns=("dbo.widgets", "dbo.orders"),
        object_reasons=(
            FastPathObjectReason(object_fqn="dbo.widgets", supporting_columns=("id", "name")),
            FastPathObjectReason(object_fqn="dbo.orders", supporting_columns=("id", "name")),
        ),
        confirmed_relationships_used=("dbo.orders.WidgetID -> dbo.widgets.id",),
        sql="SELECT w.name FROM dbo.widgets w JOIN dbo.orders o ON o.WidgetID = w.id",
        clarification_question=None, response_text=None,
        decision_evidence=("direct match",), confidence=0.9,
        follow_up_resolution=FastPathFollowUpResolution(
            resolution_type=RESOLUTION_TYPE_CONTINUATION, resolved_standalone_question="Only active widgets.",
        ),
    )
    base.update(overrides)
    return FastPathGenerationCandidate(**base)


def test_next_turn_context_from_candidate_dedupes_columns_across_objects():
    candidate = _candidate()
    ctx = next_turn_context_from_candidate(candidate)
    assert ctx["selected_object_fqns"] == ("dbo.widgets", "dbo.orders")
    assert ctx["selected_output_columns"] == ("id", "name")  # deduped, order preserved
    assert ctx["filters"] == ("active = 1",)
    assert ctx["operation"] == "LIST"
    assert ctx["requested_attributes"] == ("name",)


def test_build_prior_turn_context_from_answered_state():
    state_dict = build_answered_state(
        source_id=SOURCE_ID, build_id=1, schema_fingerprint="fp", last_question="Only active widgets.",
        now=datetime.now(timezone.utc),
        **next_turn_context_from_candidate(_candidate()),
    )
    state = parse_new_pipeline_state(state_dict, source_id=SOURCE_ID)
    assert state is not None

    prior_turn = build_prior_turn_context(state, conversation_id="conv-1", user_id="user-1")

    assert prior_turn.resolved_standalone_question == "Only active widgets."
    assert prior_turn.selected_object_fqns == ("dbo.widgets", "dbo.orders")
    assert prior_turn.selected_output_columns == ("id", "name")
    assert prior_turn.filters == ("active = 1",)
    assert prior_turn.is_clarification_resume is False
    assert prior_turn.pending_clarification_question is None
    assert prior_turn.conversation_id == "conv-1"


def test_build_prior_turn_context_from_clarification_pending_state():
    state_dict = build_clarification_state(
        source_id=SOURCE_ID, build_id=1, schema_fingerprint="fp", last_question="How much did they pay?",
        pending_clarification_question="SQL course fees or IPLAN installment payments?", clarification_turns=1,
        now=datetime.now(timezone.utc),
    )
    state = parse_new_pipeline_state(state_dict, source_id=SOURCE_ID)
    assert state is not None

    prior_turn = build_prior_turn_context(state)

    assert prior_turn.is_clarification_resume is True
    assert prior_turn.pending_clarification_question == "SQL course fees or IPLAN installment payments?"
    # No SQL plan exists yet for a pending clarification -- structural
    # fields stay at their empty defaults.
    assert prior_turn.selected_object_fqns == ()


def test_render_prior_turn_context_text_never_includes_row_values():
    """The follow-up contract: structure only, never returned data. This
    is a structural guarantee by construction (FastPathPriorTurnContext has
    no row/value field to render in the first place) -- this test pins
    that the rendered text only ever contains the bounded fields, not an
    accidental leak of something row-shaped."""
    candidate = _candidate()
    state_dict = build_answered_state(
        source_id=SOURCE_ID, build_id=1, schema_fingerprint="fp", last_question="Only active widgets.",
        now=datetime.now(timezone.utc), **next_turn_context_from_candidate(candidate),
    )
    state = parse_new_pipeline_state(state_dict, source_id=SOURCE_ID)
    text = render_prior_turn_context_text(build_prior_turn_context(state))

    assert "PREVIOUS_RESOLVED_QUESTION: Only active widgets." in text
    assert "PREVIOUS_SELECTED_OBJECTS: dbo.widgets, dbo.orders" in text
    assert "PREVIOUS_OUTPUT_COLUMNS: id, name" in text
    assert "PREVIOUS_OPERATION: LIST" in text
    assert "PREVIOUS_FILTERS: active = 1" in text


def test_render_prior_turn_context_text_includes_clarification_marker():
    state_dict = build_clarification_state(
        source_id=SOURCE_ID, build_id=1, schema_fingerprint="fp", last_question="How much did they pay?",
        pending_clarification_question="SQL course fees or IPLAN installment payments?", clarification_turns=1,
        now=datetime.now(timezone.utc),
    )
    state = parse_new_pipeline_state(state_dict, source_id=SOURCE_ID)
    text = render_prior_turn_context_text(build_prior_turn_context(state))

    assert "PENDING_CLARIFICATION_QUESTION_ASKED: SQL course fees or IPLAN installment payments?" in text
    assert "THIS_MESSAGE_IS: the user's answer to that clarification question" in text
