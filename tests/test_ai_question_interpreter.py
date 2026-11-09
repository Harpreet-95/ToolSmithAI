"""
Tests for core/semantic/ai_interpreter.py — EDP Day 1 (Modern Semantic
Understanding).

Covers:
  - Strict schema validation: valid response parses; SQL/physical-identifier
    injection, invalid enums, and malformed shapes all raise SchemaViolation.
  - interpret() fail-closed contract: no API key, no openai package, API
    exception, malformed JSON, and schema violations all return None rather
    than raising.
  - interpret() happy path: mocked OpenAI client -> validated
    AIQuestionInterpretation, JSON mode + temperature=0 enforced.
  - build_grounding_vocabulary(): bounded entity/domain name lists on
    success, empty lists on any failure (never raises).

Run:
    python -m pytest tests/test_ai_question_interpreter.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.semantic.ai_interpreter import (
    AIQuestionInterpretation,
    SchemaViolation,
    _enforce_count_language_rule,
    _validate,
    build_grounding_vocabulary,
    interpret,
)


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def test_validate_accepts_well_formed_response():
    raw = {
        "entities": ["candidates"],
        "measures": ["placements"],
        "dimensions": ["recruiter"],
        "filters": [{"attribute": "status", "operator": "=", "value": "active"}],
        "date_range": {"label": "this year", "grain": "year"},
        "sorting": {"by": "placements", "direction": "desc", "limit": 10},
        "requested_attributes": ["name", "email"],
        "relationship_intent": "list_with_related",
        "expected_result_shape": "list",
        "clarification_required": False,
    }
    result = _validate(raw)
    assert isinstance(result, AIQuestionInterpretation)
    assert result.entities == ("candidates",)
    assert result.measures == ("placements",)
    assert result.dimensions == ("recruiter",)
    assert result.sorting == {"by": "placements", "direction": "desc", "limit": 10}
    assert result.expected_result_shape == "list"
    assert result.clarification_required is False


def test_validate_minimal_response_defaults():
    result = _validate({})
    assert result.entities == ()
    assert result.expected_result_shape == "unknown"
    assert result.clarification_required is False
    assert result.date_range is None
    assert result.sorting is None


@pytest.mark.parametrize("field", ["entities", "measures", "dimensions", "requested_attributes"])
def test_validate_rejects_sql_in_term_lists(field):
    with pytest.raises(SchemaViolation):
        _validate({field: ["students; DROP TABLE students"]})


@pytest.mark.parametrize("field", ["entities", "measures", "dimensions"])
def test_validate_rejects_physical_identifier_shape(field):
    with pytest.raises(SchemaViolation):
        _validate({field: ["dbo.Students"]})


def test_validate_rejects_sql_keyword_in_filter_value():
    with pytest.raises(SchemaViolation):
        _validate({"filters": [{"attribute": "status", "operator": "=", "value": "SELECT * FROM x"}]})


def test_validate_rejects_invalid_expected_result_shape():
    with pytest.raises(SchemaViolation):
        _validate({"expected_result_shape": "sql_query"})


def test_validate_rejects_invalid_relationship_intent():
    with pytest.raises(SchemaViolation):
        _validate({"relationship_intent": "join_everything"})


# ---------------------------------------------------------------------------
# Step 5 — relationship_intent generic alias normalization.
#
# Live-confirmed gap: the model returns valid-JSON, business-meaningful
# interpretations for ranking/comparison/grouped questions, but sometimes
# labels relationship_intent with expected_result_shape's own vocabulary
# ("list", "aggregate", "count") instead of one of the four canonical
# relationship_intent values. Each case below validates a full, realistic
# executive-question-shaped payload — not just the bare relationship_intent
# field — to prove the whole interpretation survives, not just one field.
# ---------------------------------------------------------------------------

def test_validate_normal_single_entity_interpretation():
    raw = {
        "entities": ["client"],
        "filters": [{"attribute": "status", "operator": "=", "value": "active"}],
        "relationship_intent": "single_entity",
        "expected_result_shape": "list",
    }
    result = _validate(raw)
    assert result.relationship_intent == "single_entity"


def test_validate_ranked_question_normalizes_list_to_single_entity():
    """The exact live-confirmed failure shape: 'top 10 clients by revenue'
    with relationship_intent mislabeled 'list'."""
    raw = {
        "entities": ["client"],
        "measures": ["revenue"],
        "sorting": {"by": "revenue", "direction": "desc", "limit": 10},
        "relationship_intent": "list",
        "expected_result_shape": "list",
    }
    result = _validate(raw)
    assert result.relationship_intent == "single_entity"
    assert result.sorting == {"by": "revenue", "direction": "desc", "limit": 10}


def test_validate_comparison_question_normalizes_comparison_alias():
    raw = {
        "entities": ["client"],
        "measures": ["revenue"],
        "date_range": {"label": "last quarter", "grain": "quarter"},
        "relationship_intent": "comparison",
        "expected_result_shape": "aggregate",
    }
    result = _validate(raw)
    assert result.relationship_intent == "scoped_aggregate"


def test_validate_grouped_question_normalizes_grouped_alias():
    raw = {
        "entities": ["placement"],
        "dimensions": ["recruiter"],
        "relationship_intent": "grouped",
        "expected_result_shape": "list",
    }
    result = _validate(raw)
    assert result.relationship_intent == "single_entity"


def test_validate_relationship_question_still_uses_canonical_value_directly():
    """A genuine two-entity relationship question already using the correct
    canonical value must not be altered by the alias table."""
    raw = {
        "entities": ["client", "job order"],
        "filters": [{"attribute": "status", "operator": "=", "value": "open"}],
        "relationship_intent": "list_with_related",
        "expected_result_shape": "list",
    }
    result = _validate(raw)
    assert result.relationship_intent == "list_with_related"


def test_validate_malformed_relationship_intent_still_fails_closed():
    """A value that is neither canonical nor a known alias must still raise
    — the alias table does not weaken validation for genuinely untrusted or
    malformed output."""
    with pytest.raises(SchemaViolation):
        _validate({"relationship_intent": "delete_everything"})
    with pytest.raises(SchemaViolation):
        _validate({"relationship_intent": "; DROP TABLE clients"})
    with pytest.raises(SchemaViolation):
        _validate({"relationship_intent": 123})


def test_validate_rejects_invalid_date_range_grain():
    with pytest.raises(SchemaViolation):
        _validate({"date_range": {"label": "this year", "grain": "fortnight"}})


def test_validate_rejects_invalid_sort_direction():
    with pytest.raises(SchemaViolation):
        _validate({"sorting": {"by": "placements", "direction": "ascending"}})


def test_validate_rejects_non_boolean_clarification_required():
    with pytest.raises(SchemaViolation):
        _validate({"clarification_required": "yes"})


def test_validate_rejects_non_list_entities():
    with pytest.raises(SchemaViolation):
        _validate({"entities": "candidates"})


def test_validate_deduplicates_and_bounds_term_lists():
    raw = {"entities": ["candidates", "candidates", "students"] + [f"term{i}" for i in range(20)]}
    result = _validate(raw)
    assert result.entities.count("candidates") == 1
    assert len(result.entities) <= 12


# ---------------------------------------------------------------------------
# Day 2A Task 1 Part 2 — _enforce_count_language_rule (deterministic
# post-validation; contract hardening, not wired into planning)
# ---------------------------------------------------------------------------

def test_count_shape_downgraded_without_count_language():
    interp = AIQuestionInterpretation(
        entities=("invoices",), expected_result_shape="count", relationship_intent="scoped_count",
    )
    result = _enforce_count_language_rule("Active invoices", interp)
    assert result.expected_result_shape == "list"
    assert result.relationship_intent == "single_entity"


@pytest.mark.parametrize("question", [
    "How many open job orders?",
    "Count of active invoices",
    "Number of candidates",
    "Total number of placements",
    "Distinct count of recruiters",
])
def test_count_shape_kept_with_explicit_count_language(question):
    interp = AIQuestionInterpretation(expected_result_shape="count", relationship_intent="scoped_count")
    result = _enforce_count_language_rule(question, interp)
    assert result.expected_result_shape == "count"
    assert result.relationship_intent == "scoped_count"


@pytest.mark.parametrize("question", ["List open job orders", "Show active invoices", "Display candidates"])
def test_list_shape_unaffected_by_rule(question):
    interp = AIQuestionInterpretation(expected_result_shape="list", relationship_intent="list_with_related")
    result = _enforce_count_language_rule(question, interp)
    assert result.expected_result_shape == "list"
    assert result.relationship_intent == "list_with_related"


def test_aggregate_shape_untouched_by_count_language_rule():
    """Average/Total questions are governed by their own language, not this
    rule — the rule must never touch aggregate/scoped_aggregate."""
    interp = AIQuestionInterpretation(expected_result_shape="aggregate", relationship_intent="scoped_aggregate")
    result = _enforce_count_language_rule("Average invoice amount", interp)
    assert result.expected_result_shape == "aggregate"
    assert result.relationship_intent == "scoped_aggregate"


def test_count_language_rule_only_downgrades_the_field_that_needs_it():
    interp = AIQuestionInterpretation(expected_result_shape="count", relationship_intent=None)
    result = _enforce_count_language_rule("Active invoices", interp)
    assert result.expected_result_shape == "list"
    assert result.relationship_intent is None


def test_interpret_examples_present_in_system_prompt():
    from core.semantic.ai_interpreter import _SYSTEM_PROMPT
    assert "Active invoices" in _SYSTEM_PROMPT
    assert "List open job orders" in _SYSTEM_PROMPT
    assert '"expected_result_shape": "list"' in _SYSTEM_PROMPT


def test_interpret_applies_count_language_rule_end_to_end():
    """The model itself mislabeling a plain list question as a count (as
    verified live for "Active invoices") must be corrected by interpret()
    before the caller ever sees it."""
    mislabeled_response = {
        "entities": ["invoice"],
        "filters": [{"attribute": "status", "operator": "=", "value": "active"}],
        "relationship_intent": "scoped_count",
        "expected_result_shape": "count",
    }
    mock_client = _mock_client_returning(json.dumps(mislabeled_response))
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = interpret("Active invoices", api_key="sk-test")

    assert result is not None
    assert result.expected_result_shape == "list"
    assert result.relationship_intent == "single_entity"


# ---------------------------------------------------------------------------
# interpret() — fail-closed contract
# ---------------------------------------------------------------------------

def test_interpret_returns_none_without_api_key():
    assert interpret("Show active invoices", api_key="", model="gpt-4o-mini") is None


def test_interpret_returns_none_on_empty_question():
    assert interpret("", api_key="sk-test") is None
    assert interpret("   ", api_key="sk-test") is None


def test_interpret_returns_none_when_openai_package_missing():
    with patch("core.semantic.ai_interpreter._OPENAI_AVAILABLE", False):
        assert interpret("Show active invoices", api_key="sk-test") is None


def _mock_client_returning(content: str):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.choices = [MagicMock(message=MagicMock(content=content))]
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


def test_interpret_returns_none_on_malformed_json():
    mock_client = _mock_client_returning("not json")
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = interpret("Show active invoices", api_key="sk-test")
    assert result is None


def test_interpret_returns_none_on_schema_violation():
    mock_client = _mock_client_returning(json.dumps({"entities": ["dbo.Students"]}))
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = interpret("Show students", api_key="sk-test")
    assert result is None


def test_interpret_returns_none_on_api_exception():
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("rate limited")
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = interpret("Show active invoices", api_key="sk-test")
    assert result is None


# ---------------------------------------------------------------------------
# interpret() — happy path
# ---------------------------------------------------------------------------

def test_interpret_happy_path_returns_validated_interpretation():
    valid_response = {
        "entities": ["invoices"],
        "filters": [{"attribute": "status", "operator": "=", "value": "active"}],
        "expected_result_shape": "list",
        "clarification_required": False,
    }
    mock_client = _mock_client_returning(json.dumps(valid_response))
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        result = interpret("Active invoices", api_key="sk-test", model="gpt-4o-mini")

    assert result is not None
    assert result.entities == ("invoices",)
    assert result.expected_result_shape == "list"

    _, call_kwargs = mock_client.chat.completions.create.call_args
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["response_format"] == {"type": "json_object"}
    assert call_kwargs["model"] == "gpt-4o-mini"


def test_interpret_includes_known_vocabulary_in_prompt():
    mock_client = _mock_client_returning(json.dumps({"entities": ["candidates"]}))
    with patch("core.semantic.ai_interpreter._openai") as mock_openai:
        mock_openai.OpenAI.return_value = mock_client
        interpret(
            "Display candidates", api_key="sk-test",
            known_vocabulary={"entities": ["Candidate", "Placement"], "domains": ["Recruiting"]},
        )

    _, call_kwargs = mock_client.chat.completions.create.call_args
    user_message = call_kwargs["messages"][1]["content"]
    assert "Candidate" in user_message
    assert "Recruiting" in user_message


# ---------------------------------------------------------------------------
# build_grounding_vocabulary()
# ---------------------------------------------------------------------------

def test_build_grounding_vocabulary_returns_names_on_success():
    with patch("data.entity_service.get_entity_summary") as mock_entity, \
         patch("data.domain_service.get_domain_summary") as mock_domain:
        mock_entity.return_value = {"entity_counts": {"Student": 4, "Unknown": 1}}
        mock_domain.return_value = {"domain_counts": {"Enrollment": 3, "Unknown": 2}}
        vocab = build_grounding_vocabulary(source_id=1, user_id="u1")

    assert vocab["entities"] == ["Student"]
    assert vocab["domains"] == ["Enrollment"]


def test_build_grounding_vocabulary_never_raises_on_lookup_failure():
    with patch("data.entity_service.get_entity_summary", side_effect=RuntimeError("db down")), \
         patch("data.domain_service.get_domain_summary", side_effect=RuntimeError("db down")), \
         patch("data.datasource_service.get_preliminary_business_map", side_effect=RuntimeError("db down")):
        vocab = build_grounding_vocabulary(source_id=1, user_id="u1")

    assert vocab == {"entities": [], "domains": [], "preliminary_business_map": {}}
