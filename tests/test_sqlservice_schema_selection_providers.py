"""
Focused tests — Phase 7, Step 3D: the configurable schema-selection model,
strict JSON Schema Structured Outputs, and typed sanitized provider errors
added to core/sqlservice/schema_selection_providers.py.

No real OpenAI call anywhere in this file. core.sqlservice.
schema_selection_providers._openai is monkeypatched with a fake module
exposing OpenAI(**kwargs) -> a fake client whose chat.completions.create()
is scripted per test — the exact pattern already used by
tests/test_sqlservice_sql_generation.py's own
test_openai_provider_wraps_sdk_exception_without_leaking_key.
"""

from __future__ import annotations

import json

import pytest

import core.sqlservice.schema_selection_providers as providers_module
from core.sqlservice.schema_selection_providers import (
    SUPPORTED_REASONING_EFFORTS,
    OpenAISchemaSelectionProvider,
    SchemaSelectionConfigurationError,
    SchemaSelectionContentFilteredError,
    SchemaSelectionIncompleteResponseError,
    SchemaSelectionMissingStructuredOutputError,
    SchemaSelectionProviderError,
    SchemaSelectionRefusalError,
)

_VALID_SELECTED_RAW = {
    "status": "SELECTED",
    "intent_summary": {
        "operation": "COUNT", "entities": ["student"], "measures": [],
        "filters": [], "dates": [], "groupings": [],
    },
    "selected_object_fqns": ["dbo.Students"],
    "object_reasons": [
        {"object_fqn": "dbo.Students", "supported_requirements": ["has students"], "supporting_columns": ["id"]}
    ],
    "clarification_question": None,
}

_VALID_CLARIFICATION_RAW = {
    "status": "CLARIFICATION",
    "intent_summary": {
        "operation": "LIST", "entities": [], "measures": [],
        "filters": [], "dates": [], "groupings": [],
    },
    "selected_object_fqns": [],
    "object_reasons": [],
    "clarification_question": "Which program are you asking about?",
}


class _FakeMessage:
    def __init__(self, content=None, refusal=None):
        self.content = content
        self.refusal = refusal


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, prompt_tokens=None, completion_tokens=None, total_tokens=None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _FakeResponse:
    def __init__(self, choice, model="gpt-4o-mini-2024-07-18", usage=None):
        self.choices = [choice]
        self.model = model
        self.usage = usage


class _FakeCompletions:
    def __init__(self, responder=None, exc=None):
        self._responder = responder
        self._exc = exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._responder(**kwargs)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


def _install_fake_openai(monkeypatch, *, responder=None, exc=None, expected_api_key=None):
    completions = _FakeCompletions(responder=responder, exc=exc)

    class _FakeOpenAIModule:
        @staticmethod
        def OpenAI(**kwargs):
            if expected_api_key is not None:
                assert kwargs.get("api_key") == expected_api_key
            return _FakeClient(completions)

    monkeypatch.setattr(providers_module, "_openai", _FakeOpenAIModule())
    monkeypatch.setattr(providers_module, "_OPENAI_AVAILABLE", True)
    return completions


def _responder_returning(raw: dict, *, model="gpt-4o-mini-2024-07-18"):
    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content=json.dumps(raw))), model=model)
    return _respond


# ── 1. Selector model comes from dedicated configuration ──────────────────

def test_model_defaults_to_dedicated_config_not_openai_model(monkeypatch):
    monkeypatch.setattr(providers_module, "AI_FIRST_SCHEMA_SELECTION_MODEL", "gpt-6-astra")
    provider = OpenAISchemaSelectionProvider(api_key="k")
    assert provider.model == "gpt-6-astra"


def test_explicit_model_argument_overrides_config(monkeypatch):
    monkeypatch.setattr(providers_module, "AI_FIRST_SCHEMA_SELECTION_MODEL", "gpt-6-astra")
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")
    assert provider.model == "gpt-4o-mini"


# ── 2. Reasoning effort forwarded correctly ────────────────────────────────

def test_reasoning_effort_forwarded_to_provider_call(monkeypatch):
    completions = _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-6-astra", reasoning_effort="medium")

    provider.select_schema("system", "user", max_output_tokens=500)

    assert len(completions.calls) == 1
    assert completions.calls[0]["reasoning_effort"] == "medium"


def test_reasoning_effort_omitted_from_call_when_not_configured(monkeypatch):
    # A constructor reasoning_effort of None means "fall back to core.config.
    # AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT" (see this provider's own
    # docstring) — Phase 7, Step 4 promoted that variable to "medium" as
    # this environment's own local .env default, so this test must patch
    # the module-level name schema_selection_providers.py itself imported
    # (not core.config, which this module never re-reads after import) back
    # to None to prove the constructor's OWN omission behavior, independent
    # of whatever any particular deployment's .env currently says.
    monkeypatch.setattr(providers_module, "AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT", None)
    completions = _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    provider.select_schema("system", "user", max_output_tokens=500)

    assert "reasoning_effort" not in completions.calls[0]


def test_all_supported_reasoning_efforts_accepted(monkeypatch):
    for effort in SUPPORTED_REASONING_EFFORTS:
        completions = _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))
        provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-6-astra", reasoning_effort=effort)
        provider.select_schema("system", "user", max_output_tokens=500)
        assert completions.calls[0]["reasoning_effort"] == effort


# ── 3. Unsupported reasoning effort rejected before a provider call ───────

def test_unsupported_reasoning_effort_rejected_before_provider_call(monkeypatch):
    completions = _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))

    with pytest.raises(SchemaSelectionConfigurationError) as excinfo:
        OpenAISchemaSelectionProvider(api_key="k", model="gpt-6-astra", reasoning_effort="ultra-max")

    assert "ultra-max" in str(excinfo.value)
    assert completions.calls == []  # constructor raised -- zero provider calls made


# ── 4/5. Strict JSON Schema sent to the provider; forbids additional
#         properties throughout ──────────────────────────────────────────

def _assert_no_additional_properties(schema: dict) -> None:
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False
        for prop_schema in schema.get("properties", {}).values():
            _assert_no_additional_properties(prop_schema)
    if schema.get("type") == "array":
        _assert_no_additional_properties(schema.get("items", {}))


def test_strict_json_schema_structured_outputs_sent(monkeypatch):
    completions = _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    provider.select_schema("system", "user", max_output_tokens=500)

    response_format = completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert set(schema["required"]) == {
        "status", "intent_summary", "selected_object_fqns", "object_reasons", "clarification_question",
        "response_text",
    }
    assert set(schema["properties"]["status"]["enum"]) == {"SELECTED", "CLARIFICATION", "NON_DATA", "UNSUPPORTED"}
    _assert_no_additional_properties(schema)


# ── 6/7. SELECTED and CLARIFICATION responses parse correctly ─────────────

def test_selected_response_parses_correctly(monkeypatch):
    _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW, model="gpt-6-astra-2026-09-01"))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-6-astra")

    result = provider.select_schema("system", "user", max_output_tokens=500)

    assert result.raw == _VALID_SELECTED_RAW
    assert result.model == "gpt-6-astra-2026-09-01"


def test_clarification_response_parses_correctly(monkeypatch):
    _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_CLARIFICATION_RAW))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    result = provider.select_schema("system", "user", max_output_tokens=500)

    assert result.raw == _VALID_CLARIFICATION_RAW


# ── 8. Refusal becomes sanitized provider failure ──────────────────────────

def test_refusal_becomes_sanitized_provider_failure(monkeypatch):
    secret_refusal_text = "I cannot help because internal-detail-XYZ was requested"

    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content=None, refusal=secret_refusal_text)))

    completions = _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionRefusalError) as excinfo:
        provider.select_schema("system", "user", max_output_tokens=500)

    assert secret_refusal_text not in str(excinfo.value)
    assert len(completions.calls) == 1  # zero retries


# ── 9. Incomplete/truncated response fails closed ──────────────────────────

def test_truncated_response_fails_closed(monkeypatch):
    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content='{"partial": '), finish_reason="length"))

    completions = _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionIncompleteResponseError) as excinfo:
        provider.select_schema("system", "user", max_output_tokens=500)

    assert '"partial"' not in str(excinfo.value)
    assert len(completions.calls) == 1


# ── 10. Content-filtered response fails closed ─────────────────────────────

def test_content_filtered_response_fails_closed(monkeypatch):
    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content=None), finish_reason="content_filter"))

    completions = _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionContentFilteredError):
        provider.select_schema("system", "user", max_output_tokens=500)
    assert len(completions.calls) == 1


def test_missing_structured_output_fails_closed(monkeypatch):
    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content=""), finish_reason="stop"))

    _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionMissingStructuredOutputError):
        provider.select_schema("system", "user", max_output_tokens=500)


# ── 11. Actual response model recorded ──────────────────────────────────────

def test_actual_response_model_recorded_even_when_different_from_requested(monkeypatch):
    _install_fake_openai(
        monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW, model="gpt-6-astra-snapshot-2026-09-20"),
    )
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-6-astra")

    result = provider.select_schema("system", "user", max_output_tokens=500)

    assert provider.model == "gpt-6-astra"
    assert result.model == "gpt-6-astra-snapshot-2026-09-20"


# ── Safe token-usage extraction (integers only, never raw text) ────────────

def test_usage_extracted_when_present(monkeypatch):
    def _respond(**kwargs):
        return _FakeResponse(
            _FakeChoice(_FakeMessage(content=json.dumps(_VALID_SELECTED_RAW))),
            usage=_FakeUsage(prompt_tokens=1000, completion_tokens=50, total_tokens=1050),
        )

    _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    result = provider.select_schema("system", "user", max_output_tokens=500)

    assert result.usage == {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}
    assert all(isinstance(v, int) for v in result.usage.values())


def test_usage_none_when_not_exposed(monkeypatch):
    _install_fake_openai(monkeypatch, responder=_responder_returning(_VALID_SELECTED_RAW))
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    result = provider.select_schema("system", "user", max_output_tokens=500)

    assert result.usage is None


# ── 12. No API key, prompt, or raw response leakage ─────────────────────────

def test_no_api_key_prompt_or_raw_response_leakage_on_sdk_exception(monkeypatch):
    secret_key = "sk-super-secret-value-should-never-appear-anywhere"
    secret_prompt_fragment = "CONFIDENTIAL_SYSTEM_PROMPT_MARKER"

    _install_fake_openai(monkeypatch, exc=RuntimeError("connection reset by peer"), expected_api_key=secret_key)
    provider = OpenAISchemaSelectionProvider(api_key=secret_key, model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionProviderError) as excinfo:
        provider.select_schema(f"system {secret_prompt_fragment}", "user", max_output_tokens=500)

    assert secret_key not in str(excinfo.value)
    assert secret_key not in repr(excinfo.value)
    assert secret_prompt_fragment not in str(excinfo.value)


def test_no_raw_response_leakage_on_refusal_or_truncation(monkeypatch):
    secret_marker = "RAW_RESPONSE_SHOULD_NEVER_LEAK"

    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(content=None, refusal=secret_marker)))

    _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionRefusalError) as excinfo:
        provider.select_schema("system", "user", max_output_tokens=500)
    assert secret_marker not in str(excinfo.value)


# ── 13. Exactly one provider call ───────────────────────────────────────────

@pytest.mark.parametrize(
    "respond_kwargs",
    [
        dict(content=None, refusal="no"),
        dict(content=None, finish_reason="content_filter"),
        dict(content="", finish_reason="length"),
        dict(content=""),
    ],
)
def test_exactly_one_provider_call_regardless_of_outcome(monkeypatch, respond_kwargs):
    finish_reason = respond_kwargs.pop("finish_reason", "stop")

    def _respond(**kwargs):
        return _FakeResponse(_FakeChoice(_FakeMessage(**respond_kwargs), finish_reason=finish_reason))

    completions = _install_fake_openai(monkeypatch, responder=_respond)
    provider = OpenAISchemaSelectionProvider(api_key="k", model="gpt-4o-mini")

    with pytest.raises(SchemaSelectionProviderError):
        provider.select_schema("system", "user", max_output_tokens=500)

    assert len(completions.calls) == 1


# ── Configuration independence from other providers ─────────────────────────

def test_selector_model_independent_of_openai_model(monkeypatch):
    monkeypatch.setattr(providers_module, "AI_FIRST_SCHEMA_SELECTION_MODEL", "gpt-6-astra")
    import core.config as config_module
    monkeypatch.setattr(config_module, "OPENAI_MODEL", "gpt-4o-mini")

    provider = OpenAISchemaSelectionProvider(api_key="k")
    assert provider.model == "gpt-6-astra"
