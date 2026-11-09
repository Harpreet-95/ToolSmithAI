"""
Focused tests — Phase 7, Step 6C, Part 3: the fast path's combined
generation stage (core/sqlservice/fast_path_generation_service.py).

Pure/in-memory: contexts are hand-built core.sqlservice.fast_path_context.
FastPathContext objects wrapping hand-built SchemaFrontierResult objects.
No SQLite, no real embedding, no real OpenAI call anywhere in this file.
"""

import pytest

from core.sqlservice.fast_path_context import STATUS_OK as CONTEXT_STATUS_OK
from core.sqlservice.fast_path_context import FastPathContext, render_fast_path_candidate
from core.sqlservice.fast_path_generation_providers import (
    FastPathGenerationProvider,
    ProviderFastPathGenerationResponse,
)
from core.sqlservice.fast_path_generation_service import (
    REQUEST_KIND_CLARIFICATION_REQUIRED,
    REQUEST_KIND_DATA_QUESTION,
    REQUEST_KIND_NON_DATA,
    REQUEST_KIND_UNSUPPORTED,
    RESOLUTION_TYPE_CONTINUATION,
    RESOLUTION_TYPE_NONE,
    RESOLUTION_TYPE_UNRELATED,
    STATUS_INVALID_CONTEXT,
    STATUS_OK,
    FastPathGenerationValidationError,
    build_fast_path_prompts,
    generate_fast_path_candidate,
)
from core.sqlservice.fast_path_prior_turn import FastPathPriorTurnContext
from core.sqlservice.schema_frontier import STATUS_OK as FRONTIER_STATUS_OK
from core.sqlservice.schema_frontier import FrontierCandidate, SchemaFrontierResult


def _candidate(fqn, **overrides):
    base = dict(
        object_fqn=fqn, object_type="TABLE", retrieval_tier=1,
        business_name=None, business_purpose=None, domain=None, aliases=(),
        primary_key_columns=("id",), foreign_key_summary=(), local_foreign_key_columns=(),
        measure_columns=(), date_columns=(), status_columns=(), dimension_columns=("id", "name"),
        confirmed_relationship_neighbors=(), semantic_rank=1, semantic_score=0.9,
        lexical_rank=None, lexical_score=None, business_concept_rank=None,
        business_concept_score=None, canonical_rank=None, canonical_total_score=None,
        fused_rank=1, fused_score=0.05, admitted_via_lanes=("semantic",),
        matched_business_concepts=(),
    )
    base.update(overrides)
    return FrontierCandidate(**base)


def _frontier(*candidates):
    return SchemaFrontierResult(
        status=FRONTIER_STATUS_OK, build_id=1, schema_fingerprint="fp", embedding_provider="fake",
        embedding_model="fake-model", embedding_dimensions=4, candidates=tuple(candidates),
        total_characters=100, truncated=False, warnings=(), error=None,
    )


def _context(*candidates):
    frontier = _frontier(*candidates)
    text = "\n\n".join(render_fast_path_candidate(c) for c in frontier.candidates)
    return FastPathContext(
        status=CONTEXT_STATUS_OK, build_id=1, schema_fingerprint="fp", frontier=frontier, context_text=text,
        object_count=len(frontier.candidates), total_characters=len(text),
        estimated_tokens=len(text) // 4, warnings=(), error=None,
    )


class FakeFastPathProvider(FastPathGenerationProvider):
    def __init__(self, fixed_raw=None, raise_exc=None, model="fake-model"):
        self._fixed_raw = fixed_raw
        self._raise_exc = raise_exc
        self._model = model
        self.calls: list = []

    @property
    def provider_id(self):
        return "fake"

    @property
    def model(self):
        return self._model

    def generate(self, system_prompt, user_prompt, *, max_output_tokens, timeout_seconds=None):
        self.calls.append({"system_prompt": system_prompt, "user_prompt": user_prompt})
        if self._raise_exc is not None:
            raise self._raise_exc
        return ProviderFastPathGenerationResponse(raw=self._fixed_raw, model=self._model)


def _intent():
    return {
        "operation": "LIST", "entities": ["widget"], "measures": [], "filters": [],
        "date_constraints": [], "groupings": [], "requested_attributes": [],
    }


def _none_resolution(question: str = "placeholder"):
    return {"resolution_type": "none", "resolved_standalone_question": question}


def _data_question_raw(selected=("dbo.widgets",), column="id", sql="SELECT id FROM dbo.widgets", relationships=()):
    return {
        "request_kind": REQUEST_KIND_DATA_QUESTION, "intent": _intent(),
        "selected_object_fqns": list(selected),
        "object_reasons": [{"object_fqn": fqn, "supporting_columns": [column]} for fqn in selected],
        "confirmed_relationships_used": list(relationships), "sql": sql,
        "clarification_question": None, "response_text": None,
        "decision_evidence": ["direct match"], "confidence": 0.9,
        "follow_up_resolution": _none_resolution(),
    }


# ── 1. Successful DATA_QUESTION parses correctly ────────────────────────


def test_data_question_parses_correctly():
    context = _context(_candidate("dbo.widgets"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw())

    result = generate_fast_path_candidate("How many widgets?", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.request_kind == REQUEST_KIND_DATA_QUESTION
    assert result.candidate.selected_object_fqns == ("dbo.widgets",)
    assert result.candidate.sql == "SELECT id FROM dbo.widgets"
    assert len(provider.calls) == 1


# ── 2. NON_DATA / UNSUPPORTED / CLARIFICATION_REQUIRED all parse ────────


def test_non_data_parses_correctly():
    context = _context(_candidate("dbo.widgets"))
    raw = {
        "request_kind": REQUEST_KIND_NON_DATA, "intent": {**_intent(), "operation": "NONE", "entities": []},
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [], "sql": None,
        "clarification_question": None, "response_text": "Hi there!", "decision_evidence": [], "confidence": 0.9,
   
        "follow_up_resolution": _none_resolution(),
    }
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate("Hello", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.request_kind == REQUEST_KIND_NON_DATA
    assert result.candidate.response_text == "Hi there!"
    assert result.candidate.selected_object_fqns == ()


def test_unsupported_parses_correctly():
    context = _context(_candidate("dbo.widgets"))
    raw = {
        "request_kind": REQUEST_KIND_UNSUPPORTED, "intent": {**_intent(), "operation": "NONE", "entities": []},
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [], "sql": None,
        "clarification_question": None, "response_text": "Cannot delete data.", "decision_evidence": [], "confidence": 0.9,
   
        "follow_up_resolution": _none_resolution(),
    }
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate("Delete everything", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.request_kind == REQUEST_KIND_UNSUPPORTED
    assert result.candidate.sql is None


def test_clarification_required_parses_correctly():
    context = _context(_candidate("dbo.widgets"))
    raw = {
        "request_kind": REQUEST_KIND_CLARIFICATION_REQUIRED, "intent": {**_intent(), "entities": []},
        "selected_object_fqns": [], "object_reasons": [], "confirmed_relationships_used": [], "sql": None,
        "clarification_question": "Which fee?", "response_text": None, "decision_evidence": [], "confidence": 0.3,
   
        "follow_up_resolution": _none_resolution(),
    }
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate("How much did they pay?", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.request_kind == REQUEST_KIND_CLARIFICATION_REQUIRED
    assert result.candidate.clarification_question == "Which fee?"


# ── 3. Unknown object / fabricated column rejected ──────────────────────


def test_unknown_object_rejected():
    context = _context(_candidate("dbo.widgets"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw(selected=("dbo.not_real",)))

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_fabricated_column_rejected():
    context = _context(_candidate("dbo.widgets"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw(column="not_a_real_column"))

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


# ── 4. DATA_QUESTION requires non-blank SQL ─────────────────────────────


def test_data_question_requires_sql():
    context = _context(_candidate("dbo.widgets"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw(sql=""))

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_data_question_requires_non_empty_selection():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["selected_object_fqns"] = []
    raw["object_reasons"] = []
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


# ── 5. Multi-object selection requires confirmed_relationships_used ────


def test_multi_object_without_relationships_used_rejected():
    context = _context(_candidate("dbo.widgets"), _candidate("dbo.orders"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw(
        selected=("dbo.widgets", "dbo.orders"), sql="SELECT w.id FROM dbo.widgets w JOIN dbo.orders o ON w.id = o.id",
        relationships=(),
    ))

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("Widgets per order", context, provider)


def test_multi_object_with_relationships_used_accepted():
    context = _context(_candidate("dbo.widgets"), _candidate("dbo.orders"))
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw(
        selected=("dbo.widgets", "dbo.orders"), sql="SELECT w.id FROM dbo.widgets w JOIN dbo.orders o ON w.id = o.id",
        relationships=("dbo.widgets.id -> dbo.orders.widget_id",),
    ))

    result = generate_fast_path_candidate("Widgets per order", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.confirmed_relationships_used == ("dbo.widgets.id -> dbo.orders.widget_id",)


# ── 6. Cross-field invariants: sql/clarification_question/response_text
#     must be null outside their own request_kind ──────────────────────


def test_data_question_with_non_null_response_text_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["response_text"] = "should not be here"
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_non_data_with_selected_objects_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = {
        "request_kind": REQUEST_KIND_NON_DATA, "intent": {**_intent(), "operation": "NONE"},
        "selected_object_fqns": ["dbo.widgets"],
        "object_reasons": [{"object_fqn": "dbo.widgets", "supporting_columns": ["id"]}],
        "confirmed_relationships_used": [], "sql": None, "clarification_question": None,
        "response_text": "hi", "decision_evidence": [], "confidence": 0.9,
    }
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("Hello", context, provider)


# ── 7. Confidence bounds ─────────────────────────────────────────────────


def test_confidence_out_of_bounds_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["confidence"] = 1.5
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


# ── 8. Invalid context short-circuits with zero provider calls ─────────


def test_invalid_context_short_circuits():
    empty_frontier = SchemaFrontierResult(
        status=FRONTIER_STATUS_OK, build_id=1, schema_fingerprint="fp", embedding_provider="fake",
        embedding_model="fake-model", embedding_dimensions=4, candidates=(), total_characters=0,
        truncated=False, warnings=(), error=None,
    )
    context = FastPathContext(
        status=CONTEXT_STATUS_OK, build_id=1, schema_fingerprint="fp", frontier=empty_frontier, context_text="",
        object_count=0, total_characters=0, estimated_tokens=0, warnings=(), error=None,
    )
    provider = FakeFastPathProvider(fixed_raw=_data_question_raw())

    result = generate_fast_path_candidate("How many widgets?", context, provider)

    assert result.status == STATUS_INVALID_CONTEXT
    assert len(provider.calls) == 0


# ── 9. Unfamiliar filter value is accepted -- filters are never checked
#     against the candidate context at all (that is what execution is
#     for, never selection). ─────────────────────────────────────────────


def test_unfamiliar_filter_value_accepted():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw(sql="SELECT id FROM dbo.widgets WHERE name = 'Zzyzx Robotics'")
    raw["intent"]["filters"] = ["name = 'Zzyzx Robotics'"]
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate("Widgets in Zzyzx Robotics", context, provider)

    assert result.status == STATUS_OK
    assert result.candidate.intent.filters == ("name = 'Zzyzx Robotics'",)


# ── 10. Phase 7, Step 6F, Part 3 — follow_up_resolution ─────────────────


def _prior_turn(**overrides):
    base = dict(
        previous_user_question="How many widgets are there?",
        resolved_standalone_question="How many widgets are there?",
        selected_object_fqns=("dbo.widgets",), selected_output_columns=("id",),
        filters=(), groupings=(), date_constraints=(), operation="COUNT",
        requested_attributes=(), pending_clarification_question=None,
        is_clarification_resume=False, schema_fingerprint="fp",
        business_knowledge_package_id=None, business_knowledge_package_version=None,
        view_profile_state=(), secret_policy_version=None, source_id=7,
        conversation_id="conv-1", user_id="user-1",
    )
    base.update(overrides)
    return FastPathPriorTurnContext(**base)


def _continuation_raw(resolved_standalone_question, **overrides):
    raw = _data_question_raw(**overrides)
    raw["follow_up_resolution"] = {
        "resolution_type": RESOLUTION_TYPE_CONTINUATION,
        "resolved_standalone_question": resolved_standalone_question,
    }
    return raw


def test_prior_turn_renders_conversation_context_section_in_prompt():
    context = _context(_candidate("dbo.widgets"))
    _, user_prompt = build_fast_path_prompts(
        "Only active ones", context, max_objects=8, prior_turn=_prior_turn(),
    )
    assert "CONVERSATION_CONTEXT" in user_prompt
    assert "How many widgets are there?" in user_prompt
    assert "dbo.widgets" in user_prompt


def test_no_prior_turn_omits_conversation_context_section():
    context = _context(_candidate("dbo.widgets"))
    _, user_prompt = build_fast_path_prompts("How many widgets?", context, max_objects=8)
    assert "CONVERSATION_CONTEXT" not in user_prompt


def test_continuation_resolution_accepted_when_prior_turn_given():
    context = _context(_candidate("dbo.widgets"))
    raw = _continuation_raw("How many active widgets are there?")
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate(
        "Only active ones", context, provider, prior_turn=_prior_turn(),
    )

    assert result.status == STATUS_OK
    assert result.candidate.follow_up_resolution.resolution_type == RESOLUTION_TYPE_CONTINUATION
    assert result.candidate.follow_up_resolution.resolved_standalone_question == "How many active widgets are there?"


def test_resolution_type_none_required_when_no_prior_turn():
    """A model hallucinating a continuation with no prior turn to continue
    is rejected -- enforced by validation, never merely requested."""
    context = _context(_candidate("dbo.widgets"))
    raw = _continuation_raw("How many active widgets are there?")
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_resolution_type_none_rejected_when_prior_turn_given():
    """The model must actually classify a real prior turn as SOME
    resolution type other than "none" -- "none" would mean it ignored the
    CONVERSATION_CONTEXT it was given entirely."""
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["follow_up_resolution"] = {"resolution_type": RESOLUTION_TYPE_NONE, "resolved_standalone_question": "x"}
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("Only active ones", context, provider, prior_turn=_prior_turn())


def test_unrelated_resolution_accepted_and_ignores_prior_objects():
    context = _context(_candidate("dbo.widgets"), _candidate("dbo.orders"))
    raw = _data_question_raw(selected=("dbo.orders",), column="id", sql="SELECT id FROM dbo.orders")
    raw["follow_up_resolution"] = {
        "resolution_type": RESOLUTION_TYPE_UNRELATED,
        "resolved_standalone_question": "How many orders are there?",
    }
    provider = FakeFastPathProvider(fixed_raw=raw)

    result = generate_fast_path_candidate(
        "How many orders are there?", context, provider, prior_turn=_prior_turn(),
    )

    assert result.status == STATUS_OK
    assert result.candidate.follow_up_resolution.resolution_type == RESOLUTION_TYPE_UNRELATED
    assert result.candidate.selected_object_fqns == ("dbo.orders",)


def test_invalid_resolution_type_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["follow_up_resolution"] = {"resolution_type": "bogus", "resolved_standalone_question": "x"}
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_blank_resolved_standalone_question_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    raw["follow_up_resolution"] = {"resolution_type": RESOLUTION_TYPE_NONE, "resolved_standalone_question": "   "}
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)


def test_missing_follow_up_resolution_key_rejected():
    context = _context(_candidate("dbo.widgets"))
    raw = _data_question_raw()
    del raw["follow_up_resolution"]
    provider = FakeFastPathProvider(fixed_raw=raw)

    with pytest.raises(FastPathGenerationValidationError):
        generate_fast_path_candidate("How many widgets?", context, provider)
