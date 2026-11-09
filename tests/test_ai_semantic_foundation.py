"""
Phase 3A — AI Semantic Intelligence Foundation: unit tests.

Covers:
  - Context construction (AISemanticContext, RelationshipSignal, ReviewTask)
  - Prompt creation (PromptBuilder)
  - Provider interface contract (AISemanticProvider → NotImplementedError)
  - JSON validation (SemanticIntelligenceService.validate_result_json)
  - Fallback behavior (no provider / unimplemented / exception → None)
  - Pipeline threshold logic (should_invoke_ai)

Run:
    python -m pytest tests/test_ai_semantic_foundation.py -v
"""
from __future__ import annotations

import pytest

from core.ai.models import AISemanticContext, AISemanticResult, RelationshipSignal, ReviewTask
from core.ai.prompt_builder import PromptBuilder
from core.ai.semantic_intelligence import AISemanticProvider, SemanticIntelligenceService


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------


def _make_context(**overrides) -> AISemanticContext:
    """Return a fully populated AISemanticContext for testing."""
    defaults: dict = dict(
        source_id=1,
        schema_name="dbo",
        table_name="students",
        table_fqn="dbo.students",
        column_name="email_address",
        business_name="Student Email",
        existing_description=None,
        existing_domain="Student Lifecycle",
        existing_entity=None,
        semantic_type="EMAIL",
        semantic_confidence=0.82,
        rule_engine_domain="Unknown",
        rule_engine_entity="Unknown",
        rule_engine_confidence=0.45,
        rule_engine_evidence=["name contains 'email'", "email_match_rate=0.94"],
        quality_score=91.0,
        quality_grade="A",
        completeness_score=98.0,
        cardinality_tier="HIGH",
        distinct_count=4850,
        distinct_percentage=97.0,
        uniqueness_score=0.97,
        null_percentage=2.0,
        empty_string_count=0,
        distribution_shape="sparse",
        pii_confirmed=True,
        pii_signals=["email_pattern", "name_heuristic"],
        dominant_pattern=r"[a-z]+\.[a-z]+@[a-z]+\.[a-z]+",
        pattern_coverage=0.94,
        email_match_rate=0.94,
        phone_match_rate=None,
        top_values=[
            {"value": "j.smith@example.edu", "count": 1, "percentage": 0.02},
            {"value": "a.jones@example.edu", "count": 1, "percentage": 0.02},
        ],
        sample_values=["j.smith@example.edu", "a.jones@example.edu"],
        relationships=[
            RelationshipSignal(table_fqn="dbo.admissions", relationship_type="referenced_by")
        ],
        review_history=[
            ReviewTask(task_id="rv-001", action="approved", reviewed_at="2026-01-15")
        ],
        existing_dictionary=None,
    )
    defaults.update(overrides)
    return AISemanticContext(**defaults)


def _make_result(**overrides) -> AISemanticResult:
    """Return a valid AISemanticResult for testing."""
    defaults: dict = dict(
        business_name="Student Email",
        description="Primary email address used for student communications.",
        domain="Student Lifecycle",
        entity="Student",
        confidence=0.91,
        reasoning=(
            "email pattern matches at 94% coverage",
            "PII confirmed via email_pattern signal",
            "table name 'students' strongly implies Student entity",
        ),
        review_required=True,
    )
    defaults.update(overrides)
    return AISemanticResult(**defaults)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _UnimplementedProvider(AISemanticProvider):
    """Subclass that raises NotImplementedError — simulates a pending provider."""

    def analyze_metadata(self, context: AISemanticContext) -> AISemanticResult:
        raise NotImplementedError("not yet implemented")


class _WorkingProvider(AISemanticProvider):
    """Subclass that returns a preset result — simulates a wired provider."""

    def __init__(self, result: AISemanticResult) -> None:
        self._result = result

    def analyze_metadata(self, context: AISemanticContext) -> AISemanticResult:
        return self._result


class _ExplodingProvider(AISemanticProvider):
    """Subclass that raises an arbitrary runtime error — simulates a flaky provider."""

    def analyze_metadata(self, context: AISemanticContext) -> AISemanticResult:
        raise RuntimeError("simulated network timeout")


# ===========================================================================
# 1. Context Construction
# ===========================================================================


class TestContextConstruction:
    def test_fully_populated_context_instantiates(self):
        ctx = _make_context()
        assert ctx.source_id == 1
        assert ctx.table_fqn == "dbo.students"
        assert ctx.column_name == "email_address"
        assert ctx.pii_confirmed is True
        assert len(ctx.pii_signals) == 2

    def test_optional_list_fields_default_to_empty(self):
        ctx = AISemanticContext(
            source_id=2,
            schema_name="raw",
            table_name="t1",
            table_fqn="raw.t1",
            column_name="col1",
            business_name=None,
            existing_description=None,
            existing_domain=None,
            existing_entity=None,
            semantic_type=None,
            semantic_confidence=None,
            rule_engine_domain=None,
            rule_engine_entity=None,
            rule_engine_confidence=None,
        )
        assert ctx.pii_signals == []
        assert ctx.top_values == []
        assert ctx.sample_values == []
        assert ctx.relationships == []
        assert ctx.review_history == []

    def test_relationship_signal_is_frozen(self):
        rel = RelationshipSignal(table_fqn="dbo.orders", relationship_type="references")
        with pytest.raises(Exception):  # frozen dataclass
            rel.table_fqn = "dbo.other"  # type: ignore[misc]

    def test_review_task_is_frozen(self):
        rt = ReviewTask(task_id="rv-002", action="rejected", reviewed_at="2026-02-01")
        with pytest.raises(Exception):  # frozen dataclass
            rt.action = "approved"  # type: ignore[misc]

    def test_existing_dictionary_accepted_as_dict(self):
        ctx = _make_context(
            existing_dictionary={"term": "email", "definition": "student contact email"}
        )
        assert ctx.existing_dictionary is not None
        assert ctx.existing_dictionary["term"] == "email"

    def test_null_percentage_stored_correctly(self):
        ctx = _make_context(null_percentage=15.5)
        assert ctx.null_percentage == 15.5

    def test_pii_false_by_default_on_minimal_context(self):
        ctx = AISemanticContext(
            source_id=3,
            schema_name="s",
            table_name="t",
            table_fqn="s.t",
            column_name="c",
            business_name=None,
            existing_description=None,
            existing_domain=None,
            existing_entity=None,
            semantic_type=None,
            semantic_confidence=None,
            rule_engine_domain=None,
            rule_engine_entity=None,
            rule_engine_confidence=None,
        )
        assert ctx.pii_confirmed is False


# ===========================================================================
# 2. AI Semantic Result model
# ===========================================================================


class TestAISemanticResult:
    def test_valid_result_instantiates(self):
        r = _make_result()
        assert r.business_name == "Student Email"
        assert r.confidence == 0.91
        assert len(r.reasoning) == 3

    def test_confidence_zero_is_valid(self):
        r = _make_result(confidence=0.0)
        assert r.confidence == 0.0

    def test_confidence_one_is_valid(self):
        r = _make_result(confidence=1.0)
        assert r.confidence == 1.0

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            _make_result(confidence=1.01)

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            _make_result(confidence=-0.01)

    def test_empty_reasoning_raises(self):
        with pytest.raises(ValueError, match="reasoning"):
            _make_result(reasoning=())

    def test_result_is_frozen(self):
        r = _make_result()
        with pytest.raises(Exception):  # frozen dataclass
            r.business_name = "Other"  # type: ignore[misc]


# ===========================================================================
# 3. Prompt Builder
# ===========================================================================


class TestPromptBuilder:
    def setup_method(self):
        self.builder = PromptBuilder()
        self.ctx = _make_context()

    def test_system_prompt_is_non_empty_string(self):
        sp = self.builder.get_system_prompt()
        assert isinstance(sp, str) and len(sp) > 20

    def test_prompt_contains_table_name(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "students" in prompt

    def test_prompt_contains_column_name(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "email_address" in prompt

    def test_prompt_contains_pii_section_when_confirmed(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "PII" in prompt
        assert "email_pattern" in prompt

    def test_prompt_omits_pii_section_when_absent(self):
        ctx = _make_context(pii_confirmed=False, pii_signals=[])
        prompt = self.builder.build_metadata_analysis_prompt(ctx)
        assert "PII" not in prompt

    def test_prompt_contains_rule_engine_section(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "Rule Engine" in prompt
        assert "Unknown" in prompt

    def test_prompt_contains_quality_section(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "Quality" in prompt
        assert "91" in prompt

    def test_prompt_contains_output_schema(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "business_name" in prompt
        assert "review_required" in prompt
        assert "reasoning" in prompt

    def test_prompt_includes_top_values(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "j.smith@example.edu" in prompt

    def test_prompt_includes_relationships(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "dbo.admissions" in prompt

    def test_prompt_includes_review_history(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "rv-001" in prompt

    def test_prompt_includes_dictionary_when_present(self):
        ctx = _make_context(
            existing_dictionary={"term": "email", "definition": "student contact email"}
        )
        prompt = self.builder.build_metadata_analysis_prompt(ctx)
        assert "Dictionary" in prompt
        assert "student contact email" in prompt

    def test_prompt_omits_pattern_section_when_absent(self):
        ctx = _make_context(dominant_pattern=None)
        prompt = self.builder.build_metadata_analysis_prompt(ctx)
        assert "Pattern Detection" not in prompt

    def test_prompt_contains_pattern_section_when_present(self):
        prompt = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert "Pattern Detection" in prompt
        assert "0.94" in prompt

    def test_system_and_user_prompts_are_different(self):
        system_p = self.builder.get_system_prompt()
        user_p = self.builder.build_metadata_analysis_prompt(self.ctx)
        assert system_p != user_p

    def test_system_prompt_contains_anti_echo_rule(self):
        """System prompt must instruct the model not to copy the existing description."""
        sp = self.builder.get_system_prompt()
        assert "meaningfully different" in sp
        assert "Do not copy" in sp or "not copy" in sp

    def test_top_values_capped_at_ten(self):
        many_values = [
            {"value": f"val_{i}", "count": 1, "percentage": 0.1}
            for i in range(20)
        ]
        ctx = _make_context(top_values=many_values)
        prompt = self.builder.build_metadata_analysis_prompt(ctx)
        # val_10 through val_19 should be excluded
        assert "val_10" not in prompt
        assert "val_9" in prompt  # val_9 is the 10th entry (0-indexed)


# ===========================================================================
# 4. Provider Interface
# ===========================================================================


class TestProviderInterface:
    def test_abstract_provider_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            AISemanticProvider()  # type: ignore[abstract]

    def test_unimplemented_subclass_raises_not_implemented(self):
        provider = _UnimplementedProvider()
        ctx = _make_context()
        with pytest.raises(NotImplementedError):
            provider.analyze_metadata(ctx)

    def test_working_subclass_returns_result(self):
        expected = _make_result()
        provider = _WorkingProvider(expected)
        ctx = _make_context()
        out = provider.analyze_metadata(ctx)
        assert out.confidence == 0.91
        assert out.domain == "Student Lifecycle"
        assert out.review_required is True

    def test_result_returned_is_ai_semantic_result_instance(self):
        provider = _WorkingProvider(_make_result())
        out = provider.analyze_metadata(_make_context())
        assert isinstance(out, AISemanticResult)


# ===========================================================================
# 5. JSON Validation
# ===========================================================================


class TestJSONValidation:
    def _raw(self, **overrides) -> dict:
        base = {
            "business_name": "Student Email",
            "description": "Primary contact email for students.",
            "domain": "Student Lifecycle",
            "entity": "Student",
            "confidence": 0.91,
            "reasoning": ["email pattern", "PII confirmed", "context match"],
            "review_required": True,
        }
        base.update(overrides)
        return base

    def test_valid_dict_parses_to_result(self):
        result = SemanticIntelligenceService.validate_result_json(self._raw())
        assert result.business_name == "Student Email"
        assert result.confidence == 0.91
        assert len(result.reasoning) == 3
        assert result.review_required is True

    def test_missing_business_name_raises(self):
        raw = self._raw()
        del raw["business_name"]
        with pytest.raises(ValueError, match="business_name"):
            SemanticIntelligenceService.validate_result_json(raw)

    def test_missing_reasoning_raises(self):
        raw = self._raw()
        del raw["reasoning"]
        with pytest.raises(ValueError, match="reasoning"):
            SemanticIntelligenceService.validate_result_json(raw)

    def test_missing_confidence_raises(self):
        raw = self._raw()
        del raw["confidence"]
        with pytest.raises(ValueError, match="confidence"):
            SemanticIntelligenceService.validate_result_json(raw)

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            SemanticIntelligenceService.validate_result_json(self._raw(confidence=1.5))

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            SemanticIntelligenceService.validate_result_json(self._raw(confidence=-0.1))

    def test_empty_reasoning_list_raises(self):
        with pytest.raises(ValueError):
            SemanticIntelligenceService.validate_result_json(self._raw(reasoning=[]))

    def test_review_required_defaults_to_true_when_absent(self):
        raw = self._raw()
        del raw["review_required"]
        result = SemanticIntelligenceService.validate_result_json(raw)
        assert result.review_required is True

    def test_confidence_boundary_zero(self):
        result = SemanticIntelligenceService.validate_result_json(
            self._raw(confidence=0.0, reasoning=["very low confidence"])
        )
        assert result.confidence == 0.0

    def test_confidence_boundary_one(self):
        result = SemanticIntelligenceService.validate_result_json(
            self._raw(confidence=1.0)
        )
        assert result.confidence == 1.0

    def test_reasoning_coerced_to_tuple(self):
        result = SemanticIntelligenceService.validate_result_json(self._raw())
        assert isinstance(result.reasoning, tuple)

    def test_integer_reasoning_elements_coerced_to_str(self):
        result = SemanticIntelligenceService.validate_result_json(
            self._raw(reasoning=[1, 2, "three"])
        )
        assert all(isinstance(r, str) for r in result.reasoning)


# ===========================================================================
# 6. Fallback Behavior
# ===========================================================================


class TestFallbackBehavior:
    def test_no_provider_returns_none(self):
        svc = SemanticIntelligenceService(provider=None)
        result = svc.analyze(_make_context())
        assert result is None

    def test_unimplemented_provider_returns_none(self):
        svc = SemanticIntelligenceService(provider=_UnimplementedProvider())
        result = svc.analyze(_make_context())
        assert result is None

    def test_exploding_provider_returns_none(self):
        svc = SemanticIntelligenceService(provider=_ExplodingProvider())
        result = svc.analyze(_make_context())
        assert result is None

    def test_working_provider_returns_result(self):
        expected = _make_result()
        svc = SemanticIntelligenceService(provider=_WorkingProvider(expected))
        result = svc.analyze(_make_context())
        assert result is not None
        assert result.confidence == 0.91

    def test_analyze_does_not_raise_on_any_provider_error(self):
        """Service must never propagate provider exceptions to callers."""
        svc = SemanticIntelligenceService(provider=_ExplodingProvider())
        try:
            svc.analyze(_make_context())
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"analyze() raised unexpectedly: {exc}")


# ===========================================================================
# 7. Pipeline Threshold Logic
# ===========================================================================


class TestPipelineThreshold:
    def test_below_threshold_triggers_ai(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.75)
        assert svc.should_invoke_ai(0.60) is True
        assert svc.should_invoke_ai(0.74) is True
        assert svc.should_invoke_ai(0.00) is True

    def test_at_threshold_does_not_trigger_ai(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.75)
        assert svc.should_invoke_ai(0.75) is False

    def test_above_threshold_does_not_trigger_ai(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.75)
        assert svc.should_invoke_ai(0.76) is False
        assert svc.should_invoke_ai(1.00) is False

    def test_none_confidence_triggers_ai(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.75)
        assert svc.should_invoke_ai(None) is True

    def test_custom_threshold_respected(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.50)
        assert svc.should_invoke_ai(0.49) is True
        assert svc.should_invoke_ai(0.50) is False
        assert svc.should_invoke_ai(0.51) is False

    def test_threshold_property_readable(self):
        svc = SemanticIntelligenceService(confidence_threshold=0.65)
        assert svc.confidence_threshold == 0.65

    def test_build_prompt_returns_two_non_empty_strings(self):
        svc = SemanticIntelligenceService()
        system_p, user_p = svc.build_prompt(_make_context())
        assert isinstance(system_p, str) and len(system_p) > 0
        assert isinstance(user_p, str) and len(user_p) > 0

    def test_build_prompt_system_and_user_are_different(self):
        svc = SemanticIntelligenceService()
        system_p, user_p = svc.build_prompt(_make_context())
        assert system_p != user_p


# ===========================================================================
# 8. End-to-end pipeline simulation
# ===========================================================================


class TestPipelineSimulation:
    """
    Simulates the full two-stage pipeline:
      rule engine result → threshold check → AI invocation → result with review flag.
    """

    def test_high_confidence_skips_ai(self):
        """When rule engine is confident, should_invoke_ai returns False."""
        svc = SemanticIntelligenceService(
            provider=_WorkingProvider(_make_result()),
            confidence_threshold=0.75,
        )
        rule_confidence = 0.90
        assert svc.should_invoke_ai(rule_confidence) is False

    def test_low_confidence_triggers_ai_and_returns_review_flagged_result(self):
        """When rule engine confidence is low, AI runs and result must require review."""
        ai_result = _make_result(confidence=0.62, review_required=True)
        svc = SemanticIntelligenceService(
            provider=_WorkingProvider(ai_result),
            confidence_threshold=0.75,
        )
        ctx = _make_context(rule_engine_confidence=0.40)

        assert svc.should_invoke_ai(ctx.rule_engine_confidence) is True
        result = svc.analyze(ctx)

        assert result is not None
        assert result.review_required is True
        assert result.confidence == 0.62

    def test_ai_does_not_modify_context(self):
        """analyze() must not mutate the input context."""
        ctx = _make_context()
        original_column = ctx.column_name
        svc = SemanticIntelligenceService(provider=_WorkingProvider(_make_result()))
        svc.analyze(ctx)
        assert ctx.column_name == original_column
