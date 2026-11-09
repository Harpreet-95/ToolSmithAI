"""
Provider-neutral fast-path combined-call interface — Phase 7, Step 6C,
Part 3.

Structurally identical to core.sqlservice.schema_selection_providers (same
two base exception categories, same frozen unvalidated-raw response shape,
same ABC contract, same OpenAI client pattern, same strict JSON Schema
Structured Outputs usage) — deliberately NOT reused as a base class, per
this codebase's own established "one small, independent provider module per
AI call type" convention (see schema_selection_providers.py's own
docstring for the same reasoning).

This provider is intentionally thin: sends whatever system/user prompt
strings it is given, parses the response body as JSON, and hands back the
raw parsed object completely unvalidated. All prompt CONSTRUCTION and all
STRUCTURED RESPONSE validation belongs to core.sqlservice.
fast_path_generation_service — this module never inspects what the JSON
actually contains.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.config import (
    AI_FIRST_SQL_FAST_PATH_MODEL,
    AI_FIRST_SQL_FAST_PATH_REASONING_EFFORT,
    OPENAI_API_KEY,
    OPENAI_TIMEOUT_SECONDS,
)

try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


_MAX_SDK_RETRIES = 2

# Same literal set core.sqlservice.schema_selection_providers.
# SUPPORTED_REASONING_EFFORTS already validates against — duplicated per
# this codebase's own per-module-validator convention, not imported (this
# module must not depend on schema_selection_providers for its own
# correctness).
SUPPORTED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

_INTENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operation", "entities", "measures", "filters", "date_constraints", "groupings", "requested_attributes"],
    "properties": {
        "operation": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "measures": {"type": "array", "items": {"type": "string"}},
        "filters": {"type": "array", "items": {"type": "string"}},
        "date_constraints": {"type": "array", "items": {"type": "string"}},
        "groupings": {"type": "array", "items": {"type": "string"}},
        "requested_attributes": {"type": "array", "items": {"type": "string"}},
    },
}

_OBJECT_REASON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object_fqn", "supporting_columns"],
    "properties": {
        "object_fqn": {"type": "string"},
        "supporting_columns": {"type": "array", "items": {"type": "string"}},
    },
}

# Phase 7, Step 6F, Part 3 — mirrors core.sqlservice.fast_path_generation_
# service's own _REQUIRED_FOLLOW_UP_RESOLUTION_KEYS/_ALLOWED_RESOLUTION_TYPES.
_FOLLOW_UP_RESOLUTION_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["resolution_type", "resolved_standalone_question"],
    "properties": {
        "resolution_type": {
            "type": "string",
            "enum": ["none", "continuation", "clarification_answer", "comparison", "drill_down", "change", "unrelated"],
        },
        "resolved_standalone_question": {"type": "string"},
    },
}

# Phase 7, Step 6C, Part 3 — the strict JSON Schema Structured Outputs
# contract for the fast path's ONE combined call. Field names are a
# DELIBERATE, hand-kept mirror of core.sqlservice.fast_path_generation_
# service's own response-shape constants (that module is the OpenAI-
# agnostic validation layer; this schema is an OpenAI-Structured-Outputs-
# specific artifact of the SAME contract — see schema_selection_providers.
# py's own identical division of responsibility). The deterministic Python
# validator in fast_path_generation_service remains the authoritative
# boundary regardless of what this schema narrows the model to emit.
_FAST_PATH_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "request_kind", "intent", "selected_object_fqns", "object_reasons",
        "confirmed_relationships_used", "sql", "clarification_question", "response_text",
        "decision_evidence", "confidence", "follow_up_resolution",
    ],
    "properties": {
        "request_kind": {"type": "string", "enum": ["DATA_QUESTION", "CLARIFICATION_REQUIRED", "NON_DATA", "UNSUPPORTED"]},
        "intent": _INTENT_SCHEMA,
        "selected_object_fqns": {"type": "array", "items": {"type": "string"}},
        "object_reasons": {"type": "array", "items": _OBJECT_REASON_SCHEMA},
        "confirmed_relationships_used": {"type": "array", "items": {"type": "string"}},
        "sql": {"type": ["string", "null"]},
        "clarification_question": {"type": ["string", "null"]},
        "response_text": {"type": ["string", "null"]},
        "decision_evidence": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "follow_up_resolution": _FOLLOW_UP_RESOLUTION_SCHEMA,
    },
}

_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "fast_path_generation_response",
        "strict": True,
        "schema": _FAST_PATH_RESPONSE_SCHEMA,
    },
}

__all__ = [
    "SUPPORTED_REASONING_EFFORTS",
    "FastPathGenerationConfigurationError",
    "FastPathGenerationProviderError",
    "FastPathGenerationRefusalError",
    "FastPathGenerationIncompleteResponseError",
    "FastPathGenerationContentFilteredError",
    "FastPathGenerationMissingStructuredOutputError",
    "ProviderFastPathGenerationResponse",
    "FastPathGenerationProvider",
    "OpenAIFastPathGenerationProvider",
]


class FastPathGenerationConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid
    configuration (no API key, SDK not installed, an unsupported
    reasoning-effort value) — always raised before any provider call."""


class FastPathGenerationProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, or a response body that
    isn't valid JSON) — never for a configuration problem or a valid-
    JSON-but-wrong-shape response (see core.sqlservice.
    fast_path_generation_service.FastPathGenerationValidationError)."""


class FastPathGenerationRefusalError(FastPathGenerationProviderError):
    """The model declined to produce a response at all. Sanitized: never
    carries the model's own refusal text."""


class FastPathGenerationIncompleteResponseError(FastPathGenerationProviderError):
    """The provider's response was cut off before completion
    (finish_reason == "length"). Sanitized: never carries the truncated
    partial content. Never retried."""


class FastPathGenerationContentFilteredError(FastPathGenerationProviderError):
    """The provider's own content-filtering system blocked the response.
    Sanitized: never carries any provider-supplied filter detail."""


class FastPathGenerationMissingStructuredOutputError(FastPathGenerationProviderError):
    """The provider returned a normal, non-refused response with no usable
    message content at all."""


def _safe_usage(response: object) -> Mapping[str, int] | None:
    """Best-effort, integer-only token-usage extraction — never raises, and
    never returns anything but small plain ints. Same shape as
    core.sqlservice.schema_selection_providers._safe_usage."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    result: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            result[field] = value
    return result or None


@dataclass(frozen=True)
class ProviderFastPathGenerationResponse:
    """Result of one FastPathGenerationProvider.generate() call. `raw` is
    the response body parsed as JSON — completely unvalidated;
    core.sqlservice.fast_path_generation_service is responsible for
    checking its shape before trusting it."""

    raw: Mapping[str, object]
    model: str
    usage: Mapping[str, int] | None = None


class FastPathGenerationProvider(ABC):
    """Abstract interface for the fast path's one combined structured AI
    call (request kind + intent + schema selection + SQL generation)."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderFastPathGenerationResponse:
        """
        Send one system/user prompt pair and return the parsed JSON body.

        Raises:
            FastPathGenerationConfigurationError: provider is not
                configured to run.
            FastPathGenerationProviderError: the provider call failed, or
                its response body was not valid JSON. May be one of the
                typed subclasses for a specific, never-retried outcome.

        Implementations must NOT catch and swallow any of the above, and
        must NEVER retry internally.
        """
        raise NotImplementedError


class OpenAIFastPathGenerationProvider(FastPathGenerationProvider):
    """OpenAI implementation, same client pattern as core.sqlservice.
    schema_selection_providers.OpenAISchemaSelectionProvider (one
    synchronous OpenAI() client per call, explicit bounded SDK-level
    max_retries, strict JSON Schema Structured Outputs).

    `model`/`reasoning_effort` default to core.config.
    AI_FIRST_SQL_FAST_PATH_MODEL/AI_FIRST_SQL_FAST_PATH_REASONING_EFFORT —
    its own dedicated configuration pair, independent of every other
    AI_FIRST_* model pair.
    """

    _PROVIDER_ID = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._api_key: str = api_key if api_key is not None else OPENAI_API_KEY
        self._model: str = model or AI_FIRST_SQL_FAST_PATH_MODEL
        self._timeout: int = timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        self._reasoning_effort: str | None = (
            reasoning_effort if reasoning_effort is not None else AI_FIRST_SQL_FAST_PATH_REASONING_EFFORT
        )
        if self._reasoning_effort is not None and self._reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise FastPathGenerationConfigurationError(
                f"unsupported reasoning_effort {self._reasoning_effort!r}; must be one of "
                f"{sorted(SUPPORTED_REASONING_EFFORTS)} or None"
            )

    @property
    def provider_id(self) -> str:
        return self._PROVIDER_ID

    @property
    def model(self) -> str:
        return self._model

    @property
    def reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderFastPathGenerationResponse:
        self._assert_available()

        client = _openai.OpenAI(
            api_key=self._api_key,
            timeout=timeout_seconds if timeout_seconds is not None else self._timeout,
            max_retries=_MAX_SDK_RETRIES,
        )
        create_kwargs: dict = dict(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=max_output_tokens,
            response_format=_RESPONSE_FORMAT,
        )
        if self._reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            create_kwargs["temperature"] = 0
        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            raise FastPathGenerationProviderError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        if getattr(message, "refusal", None):
            raise FastPathGenerationRefusalError(
                "the fast-path generation provider declined to produce a structured response"
            )
        if finish_reason == "content_filter":
            raise FastPathGenerationContentFilteredError(
                "the fast-path generation provider's content-filtering system blocked the response"
            )
        if finish_reason == "length":
            raise FastPathGenerationIncompleteResponseError(
                "the fast-path generation provider's response was truncated before completion "
                "(finish_reason='length') -- raise max_output_tokens or reduce the context size"
            )

        raw_content = message.content or ""
        if not raw_content.strip():
            raise FastPathGenerationMissingStructuredOutputError(
                "the fast-path generation provider returned no structured output content"
            )
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise FastPathGenerationProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderFastPathGenerationResponse(raw=parsed, model=response.model, usage=_safe_usage(response))

    def _assert_available(self) -> None:
        if not self._api_key:
            raise FastPathGenerationConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable fast-path generation."
            )
        if not _OPENAI_AVAILABLE:
            raise FastPathGenerationConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
