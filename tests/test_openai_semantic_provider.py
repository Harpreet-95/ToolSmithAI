"""
Tests for core/ai/providers/openai_provider.py

Covers:
  - No API key → RuntimeError (→ service returns None)
  - Missing openai package → RuntimeError (→ service returns None)
  - Valid mocked JSON response → AISemanticResult with correct fields
  - review_required remains True in result
  - PromptBuilder is called with the context
  - OpenAI client created with correct params (api_key, timeout, model)
  - JSON mode (response_format) and temperature=0 are enforced
  - Malformed JSON → JSONDecodeError (→ service returns None)
  - API exception → propagates (→ service returns None)
  - All failure modes return None from SemanticIntelligenceService

Run:
    python -m pytest tests/test_openai_semantic_provider.py tests/test_ai_semantic_foundation.py -v
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest

from core.ai.models import AISemanticContext, AISemanticResult, RelationshipSignal, ReviewTask
from core.ai.providers.openai_provider import OpenAISemanticProvider
from core.ai.semantic_intelligence import SemanticIntelligenceService


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_context(**overrides) -> AISemanticContext:
    defaults = dict(
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
        rule_engine_evidence=["name contains 'email'"],
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
        top_values=[{"value": "j.smith@example.edu", "count": 1, "percentage": 0.02}],
        sample_values=["j.smith@example.edu", "a.jones@example.edu"],
        relationships=[RelationshipSignal(table_fqn="dbo.admissions", relationship_type="referenced_by")],
        review_history=[ReviewTask(task_id="rv-001", action="approved", reviewed_at="2026-01-15")],
        existing_dictionary=None,
    )
    defaults.update(overrides)
    return AISemanticContext(**defaults)


_VALID_AI_RESPONSE: dict = {
    "business_name": "Student Email",
    "description": "Primary email address used for student communications.",
    "domain": "Student Lifecycle",
    "entity": "Student",
    "confidence": 0.91,
    "reasoning": [
        "email_match_rate=0.94 strongly indicates an email column",
        "PII confirmed via email_pattern and name_heuristic signals",
        "table name 'students' aligns with Student entity",
    ],
    "review_required": True,
}


def _mock_openai_module(response_content: str) -> MagicMock:
    """
    Build a MagicMock that looks like the openai module.

    mock.OpenAI(...).chat.completions.create(...) returns a response whose
    choices[0].message.content equals *response_content*.
    """
    msg = MagicMock()
    msg.content = response_content

    choice = MagicMock()
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]

    client = MagicMock()
    client.chat.completions.create.return_value = response

    openai_module = MagicMock()
    openai_module.OpenAI.return_value = client

    return openai_module


def _provider(api_key: str = "sk-test-key-abc123") -> OpenAISemanticProvider:
    """Return a provider with the given key and fast timeout for testing."""
    return OpenAISemanticProvider(api_key=api_key, model="gpt-4o-mini", timeout_seconds=5)


# ===========================================================================
# 1. Constructor — config reading
# ===========================================================================


class TestConstructorConfig:
    def test_reads_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key-xyz")
        provider = OpenAISemanticProvider()
        assert provider._api_key == "env-key-xyz"

    def test_reads_model_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
        provider = OpenAISemanticProvider(api_key="k")
        assert provider._model == "gpt-4o"

    def test_reads_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("AI_SEMANTIC_TIMEOUT_SECONDS", "30")
        provider = OpenAISemanticProvider(api_key="k")
        assert provider._timeout == 30

    def test_constructor_injection_overrides_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        monkeypatch.setenv("OPENAI_MODEL", "env-model")
        monkeypatch.setenv("AI_SEMANTIC_TIMEOUT_SECONDS", "99")
        provider = OpenAISemanticProvider(api_key="injected-key", model="gpt-4o", timeout_seconds=7)
        assert provider._api_key == "injected-key"
        assert provider._model == "gpt-4o"
        assert provider._timeout == 7

    def test_empty_api_key_stored_as_empty_string(self):
        provider = OpenAISemanticProvider(api_key="")
        assert provider._api_key == ""

    def test_default_model_is_gpt4o_mini(self, monkeypatch):
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        provider = OpenAISemanticProvider(api_key="k")
        assert provider._model == "gpt-4o-mini"

    def test_default_timeout_is_fifteen(self, monkeypatch):
        monkeypatch.delenv("AI_SEMANTIC_TIMEOUT_SECONDS", raising=False)
        provider = OpenAISemanticProvider(api_key="k")
        assert provider._timeout == 15


# ===========================================================================
# 2. Safety — missing API key
# ===========================================================================


class TestMissingApiKey:
    def test_no_api_key_raises_runtime_error(self):
        provider = _provider(api_key="")
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            provider.analyze_metadata(_make_context())

    def test_no_api_key_message_is_actionable(self):
        provider = _provider(api_key="")
        with pytest.raises(RuntimeError) as exc_info:
            provider.analyze_metadata(_make_context())
        assert ".env" in str(exc_info.value) or "OPENAI_API_KEY" in str(exc_info.value)

    def test_no_api_key_does_not_call_openai(self):
        provider = _provider(api_key="")
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                with pytest.raises(RuntimeError):
                    provider.analyze_metadata(_make_context())
        mock_openai.OpenAI.assert_not_called()

    def test_service_returns_none_when_no_api_key(self):
        svc = SemanticIntelligenceService(provider=_provider(api_key=""))
        result = svc.analyze(_make_context())
        assert result is None


# ===========================================================================
# 3. Safety — missing openai package
# ===========================================================================


class TestMissingOpenAIPackage:
    def test_unavailable_package_raises_runtime_error(self):
        provider = _provider()
        with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", False):
            with pytest.raises(RuntimeError, match="openai"):
                provider.analyze_metadata(_make_context())

    def test_unavailable_package_message_is_actionable(self):
        provider = _provider()
        with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", False):
            with pytest.raises(RuntimeError) as exc_info:
                provider.analyze_metadata(_make_context())
        assert "pip install" in str(exc_info.value)

    def test_service_returns_none_when_package_unavailable(self):
        svc = SemanticIntelligenceService(provider=_provider())
        with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", False):
            result = svc.analyze(_make_context())
        assert result is None


# ===========================================================================
# 4. Happy path — valid mocked response
# ===========================================================================


class TestValidResponse:
    def _call(self, response_dict: dict | None = None) -> AISemanticResult:
        provider = _provider()
        raw_json = json.dumps(response_dict or _VALID_AI_RESPONSE)
        mock_openai = _mock_openai_module(raw_json)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                return provider.analyze_metadata(_make_context())

    def test_returns_ai_semantic_result_instance(self):
        assert isinstance(self._call(), AISemanticResult)

    def test_business_name_matches_response(self):
        assert self._call().business_name == "Student Email"

    def test_description_matches_response(self):
        assert "communications" in self._call().description

    def test_domain_matches_response(self):
        assert self._call().domain == "Student Lifecycle"

    def test_entity_matches_response(self):
        assert self._call().entity == "Student"

    def test_confidence_matches_response(self):
        assert self._call().confidence == 0.91

    def test_reasoning_is_non_empty_tuple(self):
        result = self._call()
        assert isinstance(result.reasoning, tuple)
        assert len(result.reasoning) == 3

    def test_review_required_is_true(self):
        assert self._call().review_required is True

    def test_review_required_remains_true_even_if_omitted_by_ai(self):
        """If AI omits review_required, validate_result_json defaults to True."""
        without_flag = {k: v for k, v in _VALID_AI_RESPONSE.items() if k != "review_required"}
        assert self._call(without_flag).review_required is True

    def test_confidence_boundary_one_accepted(self):
        perfect = {**_VALID_AI_RESPONSE, "confidence": 1.0}
        result = self._call(perfect)
        assert result.confidence == 1.0

    def test_confidence_boundary_zero_accepted(self):
        low = {**_VALID_AI_RESPONSE, "confidence": 0.0}
        result = self._call(low)
        assert result.confidence == 0.0


# ===========================================================================
# 5. PromptBuilder is used
# ===========================================================================


class TestPromptBuilderUsage:
    def test_prompt_builder_called_with_context(self):
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        ctx = _make_context()

        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                with patch.object(
                    provider._prompt_builder,
                    "build_metadata_analysis_prompt",
                    wraps=provider._prompt_builder.build_metadata_analysis_prompt,
                ) as mock_build:
                    provider.analyze_metadata(ctx)

        mock_build.assert_called_once_with(ctx)

    def test_system_prompt_getter_called(self):
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))

        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                with patch.object(
                    provider._prompt_builder,
                    "get_system_prompt",
                    wraps=provider._prompt_builder.get_system_prompt,
                ) as mock_sys:
                    provider.analyze_metadata(_make_context())

        mock_sys.assert_called_once()

    def test_messages_sent_to_openai_include_system_and_user(self):
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))

        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())

        create_kwargs = mock_openai.OpenAI.return_value.chat.completions.create.call_args.kwargs
        messages = create_kwargs["messages"]
        roles = [m["role"] for m in messages]
        assert "system" in roles
        assert "user" in roles

    def test_user_prompt_contains_column_name(self):
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))

        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context(column_name="student_email"))

        create_kwargs = mock_openai.OpenAI.return_value.chat.completions.create.call_args.kwargs
        user_content = next(m["content"] for m in create_kwargs["messages"] if m["role"] == "user")
        assert "student_email" in user_content


# ===========================================================================
# 6. OpenAI client — request parameters
# ===========================================================================


class TestOpenAICallParameters:
    def _create_kwargs(self) -> dict:
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())
        return mock_openai.OpenAI.return_value.chat.completions.create.call_args.kwargs

    def test_response_format_is_json_object(self):
        assert self._create_kwargs()["response_format"] == {"type": "json_object"}

    def test_temperature_is_zero(self):
        assert self._create_kwargs()["temperature"] == 0

    def test_max_tokens_is_positive_and_bounded(self):
        max_tokens = self._create_kwargs()["max_tokens"]
        assert isinstance(max_tokens, int)
        assert 400 <= max_tokens <= 2000

    def test_model_matches_constructor_arg(self):
        provider = OpenAISemanticProvider(api_key="sk-test", model="gpt-4o", timeout_seconds=5)
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())
        create_kwargs = mock_openai.OpenAI.return_value.chat.completions.create.call_args.kwargs
        assert create_kwargs["model"] == "gpt-4o"

    def test_client_created_with_correct_api_key(self):
        provider = _provider(api_key="sk-specific-key")
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())
        mock_openai.OpenAI.assert_called_once_with(api_key="sk-specific-key", timeout=5)

    def test_client_created_with_correct_timeout(self):
        provider = OpenAISemanticProvider(api_key="sk-test", timeout_seconds=42)
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())
        mock_openai.OpenAI.assert_called_once_with(api_key="sk-test", timeout=42)


# ===========================================================================
# 7. Malformed / invalid JSON responses
# ===========================================================================


class TestMalformedResponse:
    def _call_with_content(self, content: str) -> None:
        provider = _provider()
        mock_openai = _mock_openai_module(content)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                provider.analyze_metadata(_make_context())

    def test_not_json_raises_json_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            self._call_with_content("this is not json at all")

    def test_partial_json_raises_json_decode_error(self):
        with pytest.raises(json.JSONDecodeError):
            self._call_with_content('{"business_name": "X"')  # unclosed brace

    def test_valid_json_but_missing_keys_raises_value_error(self):
        incomplete = json.dumps({"business_name": "X", "description": "Y"})
        with pytest.raises(ValueError, match="missing required keys"):
            self._call_with_content(incomplete)

    def test_confidence_out_of_range_raises_value_error(self):
        bad = {**_VALID_AI_RESPONSE, "confidence": 1.5}
        with pytest.raises(ValueError, match="confidence"):
            self._call_with_content(json.dumps(bad))

    def test_empty_reasoning_raises_value_error(self):
        bad = {**_VALID_AI_RESPONSE, "reasoning": []}
        with pytest.raises(ValueError):
            self._call_with_content(json.dumps(bad))

    def test_empty_response_content_raises(self):
        with pytest.raises(json.JSONDecodeError):
            self._call_with_content("")

    def test_service_returns_none_on_malformed_json(self):
        provider = _provider()
        mock_openai = _mock_openai_module("not json {{{")
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(_make_context())
        assert result is None

    def test_service_returns_none_on_invalid_schema(self):
        provider = _provider()
        incomplete = json.dumps({"business_name": "X"})
        mock_openai = _mock_openai_module(incomplete)
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(_make_context())
        assert result is None


# ===========================================================================
# 8. API exceptions
# ===========================================================================


class TestAPIExceptions:
    def _provider_with_failing_client(self, error: Exception) -> tuple[OpenAISemanticProvider, MagicMock]:
        provider = _provider()
        mock_openai = MagicMock()
        mock_openai.OpenAI.return_value.chat.completions.create.side_effect = error
        return provider, mock_openai

    def test_generic_exception_propagates_from_provider(self):
        provider, mock_openai = self._provider_with_failing_client(
            Exception("simulated network error")
        )
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                with pytest.raises(Exception, match="simulated network error"):
                    provider.analyze_metadata(_make_context())

    def test_timeout_exception_propagates_from_provider(self):
        provider, mock_openai = self._provider_with_failing_client(
            TimeoutError("request timed out")
        )
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                with pytest.raises(TimeoutError):
                    provider.analyze_metadata(_make_context())

    def test_service_returns_none_on_generic_api_error(self):
        provider, mock_openai = self._provider_with_failing_client(
            Exception("rate limit exceeded")
        )
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(_make_context())
        assert result is None

    def test_service_returns_none_on_timeout(self):
        provider, mock_openai = self._provider_with_failing_client(
            TimeoutError("timed out")
        )
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(_make_context())
        assert result is None

    def test_service_does_not_raise_on_any_api_error(self):
        provider, mock_openai = self._provider_with_failing_client(
            RuntimeError("unexpected server error")
        )
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                try:
                    svc.analyze(_make_context())
                except Exception as exc:  # pragma: no cover
                    pytest.fail(f"service.analyze() raised unexpectedly: {exc}")


# ===========================================================================
# 9. End-to-end via SemanticIntelligenceService
# ===========================================================================


class TestViaService:
    def test_full_pipeline_low_confidence_returns_ai_result(self):
        """Rule engine returns low confidence → AI runs → valid result returned."""
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps(_VALID_AI_RESPONSE))
        svc = SemanticIntelligenceService(provider=provider, confidence_threshold=0.75)
        ctx = _make_context(rule_engine_confidence=0.40)

        assert svc.should_invoke_ai(ctx.rule_engine_confidence) is True

        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(ctx)

        assert result is not None
        assert isinstance(result, AISemanticResult)
        assert result.review_required is True

    def test_provider_is_subclass_of_ai_semantic_provider(self):
        from core.ai.semantic_intelligence import AISemanticProvider
        assert issubclass(OpenAISemanticProvider, AISemanticProvider)

    def test_provider_can_be_registered_in_service(self):
        provider = _provider()
        svc = SemanticIntelligenceService(provider=provider)
        assert svc._provider is provider

    def test_result_confidence_preserved_through_service(self):
        provider = _provider()
        mock_openai = _mock_openai_module(json.dumps({**_VALID_AI_RESPONSE, "confidence": 0.62}))
        svc = SemanticIntelligenceService(provider=provider)
        with patch("core.ai.providers.openai_provider._openai", mock_openai):
            with patch("core.ai.providers.openai_provider._OPENAI_AVAILABLE", True):
                result = svc.analyze(_make_context())
        assert result is not None
        assert result.confidence == 0.62
