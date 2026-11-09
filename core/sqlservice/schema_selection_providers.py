"""
Provider-neutral AI schema-selection interface — SQL rebuild Phase 7, Step
3C, Part 3; extended in Phase 7, Step 3D with a dedicated, configurable
selector model/reasoning-effort pair and strict JSON Schema Structured
Outputs (see OpenAISchemaSelectionProvider's own docstring below).

Structurally identical to core.sqlservice.sql_generation_providers /
core.sqlservice.answer_generation_providers (same two base exception
categories, same frozen unvalidated-raw response shape, same ABC contract,
same OpenAI client pattern) — deliberately NOT reused as a base class (this
codebase's established convention is one small, independent provider module
per AI call type, not a shared provider hierarchy; see each of those two
modules' own docstrings). Never misuses core.sqlservice.sql_generation_
providers.TextToSqlProvider or core.sqlservice.answer_generation_providers.
AnswerGenerationProvider for this — schema selection is its own call type
with its own response contract.

This provider is intentionally thin: it sends whatever system/user prompt
strings it is given, parses the response body as JSON, and hands back the
raw parsed object completely unvalidated. All prompt CONSTRUCTION and all
STRUCTURED RESPONSE validation belongs to core.sqlservice.
schema_selection_service — this module never inspects what the JSON
actually contains. Step 3D adds classification of a handful of provider-
side non-2xx-but-not-an-exception outcomes (refusal, truncation, content
filtering, missing structured output) into typed, sanitized subclasses of
SchemaSelectionProviderError — still never inspecting or validating the
JSON body itself.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.config import (
    AI_FIRST_SCHEMA_SELECTION_MODEL,
    AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT,
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
_TEMPERATURE = 0  # deterministic structured output, no sampling variance

# Phase 7, Step 3D — the exact literal set the installed openai>=2.x SDK
# itself accepts for chat.completions.create's own `reasoning_effort`
# parameter (openai.types.shared.reasoning_effort.ReasoningEffort). Kept as
# an explicit local constant rather than imported from the SDK's private
# typing module — this codebase's established "validate the shape you
# actually depend on, don't trust an SDK-internal type alias to stay
# import-stable" convention (see e.g. core.sqlservice.sql_validation's own
# reasoning for hand-rolled AST guards). A value outside this set is
# rejected here, before any provider call — never silently dropped or
# substituted.
SUPPORTED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

# Phase 7, Step 3D — strict JSON Schema Structured Outputs contract for the
# schema-selection response. Field names/shape are a DELIBERATE, hand-kept
# mirror of core.sqlservice.schema_selection_service's own
# _REQUIRED_TOP_LEVEL_KEYS/_REQUIRED_INTENT_KEYS/_REQUIRED_REASON_KEYS —
# never imported from there (that module is the OpenAI-agnostic validation
# layer; this schema is an OpenAI Structured-Outputs-specific artifact of
# the SAME contract, same "one small module owns its own copy" convention
# as this file's own module docstring already establishes for the exception
# categories). If either side's field set ever changes, the other must be
# updated to match — the Python validator in schema_selection_service
# remains the authoritative, enforced boundary regardless (Structured
# Outputs narrows what the MODEL can emit; it never replaces application
# validation of what was actually emitted).
#
# Every object sets "additionalProperties": false and lists every one of
# its own properties in "required" (OpenAI strict-mode's own requirement —
# a property cannot be optional under strict:true; "clarification_question"
# models optionality the strict-mode-supported way instead, via a
# ["string", "null"] type union rather than omission). This is as far as
# JSON Schema/Structured Outputs can express the status-vs-clarification
# invariants (e.g. "selected_object_fqns must be empty when status is
# CLARIFICATION") — those cross-field invariants are, as before, enforced
# only by core.sqlservice.schema_selection_service's own Python validator.
_INTENT_SUMMARY_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operation", "entities", "measures", "filters", "dates", "groupings"],
    "properties": {
        "operation": {"type": "string"},
        "entities": {"type": "array", "items": {"type": "string"}},
        "measures": {"type": "array", "items": {"type": "string"}},
        "filters": {"type": "array", "items": {"type": "string"}},
        "dates": {"type": "array", "items": {"type": "string"}},
        "groupings": {"type": "array", "items": {"type": "string"}},
    },
}

_OBJECT_REASON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["object_fqn", "supported_requirements", "supporting_columns"],
    "properties": {
        "object_fqn": {"type": "string"},
        "supported_requirements": {"type": "array", "items": {"type": "string"}},
        "supporting_columns": {"type": "array", "items": {"type": "string"}},
    },
}

_SCHEMA_SELECTION_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "status", "intent_summary", "selected_object_fqns", "object_reasons", "clarification_question",
        "response_text",
    ],
    "properties": {
        # Phase 7, Step 6B — SELECTED/CLARIFICATION are unchanged from
        # Step 3C/3D (SELECTED means "this is a data question, objects
        # chosen"; CLARIFICATION means "data question, but genuinely
        # ambiguous"). NON_DATA/UNSUPPORTED are new: this one call now also
        # performs the request-kind judgment (DATA_QUESTION/
        # CLARIFICATION_REQUIRED/NON_DATA/UNSUPPORTED) that used to be a
        # separate, pre-pipeline keyword-based intent-router decision — see
        # core.sqlservice.schema_selection_service's module docstring.
        "status": {"type": "string", "enum": ["SELECTED", "CLARIFICATION", "NON_DATA", "UNSUPPORTED"]},
        "intent_summary": _INTENT_SUMMARY_SCHEMA,
        "selected_object_fqns": {"type": "array", "items": {"type": "string"}},
        "object_reasons": {"type": "array", "items": _OBJECT_REASON_SCHEMA},
        "clarification_question": {"type": ["string", "null"]},
        # Populated only for NON_DATA (a short conversational reply) or
        # UNSUPPORTED (a safe explanation of why this cannot be answered as
        # a data question) — null for SELECTED/CLARIFICATION.
        "response_text": {"type": ["string", "null"]},
    },
}

_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "schema_selection_response",
        "strict": True,
        "schema": _SCHEMA_SELECTION_RESPONSE_SCHEMA,
    },
}

__all__ = [
    "SUPPORTED_REASONING_EFFORTS",
    "SchemaSelectionConfigurationError",
    "SchemaSelectionProviderError",
    "SchemaSelectionRefusalError",
    "SchemaSelectionIncompleteResponseError",
    "SchemaSelectionContentFilteredError",
    "SchemaSelectionMissingStructuredOutputError",
    "ProviderSchemaSelectionResponse",
    "SchemaSelectionProvider",
    "OpenAISchemaSelectionProvider",
]


class SchemaSelectionConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (e.g. no API key, the SDK package not installed, an unsupported
    reasoning-effort value) — never a provider-side API failure and never a
    malformed-response validation failure. Always raised before any
    provider call is attempted."""


class SchemaSelectionProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, or a response body that
    isn't valid JSON at all) — wraps whatever the SDK raised. Never raised
    for a configuration problem or a valid-JSON-but-wrong-shape response
    (see core.sqlservice.schema_selection_service.
    SchemaSelectionValidationError). The four Step 3D subclasses below
    narrow this to a specific, sanitized, never-retried outcome; a caller
    that only catches this base class still catches all four unchanged."""


class SchemaSelectionRefusalError(SchemaSelectionProviderError):
    """The model declined to produce a schema-selection response at all
    (openai ChatCompletionMessage.refusal populated — Structured Outputs'
    own safety-refusal channel). Sanitized: never carries the model's own
    refusal text, which is provider output and must not be logged."""


class SchemaSelectionIncompleteResponseError(SchemaSelectionProviderError):
    """The provider's response was cut off before completion
    (finish_reason == "length", e.g. max_output_tokens was too small for
    this model/prompt). Sanitized: never carries the truncated partial
    content. Never retried — the caller must raise max_output_tokens or
    reduce the frontier size to fix this, not silently re-ask."""


class SchemaSelectionContentFilteredError(SchemaSelectionProviderError):
    """The provider's own content-filtering system blocked the response
    (finish_reason == "content_filter"). Sanitized: never carries any
    provider-supplied filter detail."""


class SchemaSelectionMissingStructuredOutputError(SchemaSelectionProviderError):
    """The provider returned a normal (finish_reason == "stop", non-refused)
    response with no usable message content at all — an infrastructure-
    level anomaly distinct from a malformed-but-present JSON body (see
    core.sqlservice.schema_selection_service.SchemaSelectionValidationError
    for that case)."""


def _safe_usage(response: object) -> Mapping[str, int] | None:
    """Best-effort, integer-only token-usage extraction from an openai
    ChatCompletion response — never raises (a missing/malformed `usage`
    attribute just yields None), and never returns anything but small
    plain ints (no nested objects, no prompt/response text)."""
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
class ProviderSchemaSelectionResponse:
    """Result of one SchemaSelectionProvider.select_schema() call. `raw` is
    the response body parsed as JSON — completely unvalidated;
    core.sqlservice.schema_selection_service is responsible for checking
    its shape before trusting it. `usage`, when the provider safely exposes
    it, is a small integer-only mapping (e.g. {"prompt_tokens": ...,
    "completion_tokens": ..., "total_tokens": ...}) for observability —
    never prompt/response text, never chain-of-thought; None when the
    provider does not expose it."""

    raw: Mapping[str, object]
    model: str
    usage: Mapping[str, int] | None = None


class SchemaSelectionProvider(ABC):
    """Abstract interface for a structured AI schema-selection call."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def select_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSchemaSelectionResponse:
        """
        Send one system/user prompt pair and return the parsed JSON body.

        Raises:
            SchemaSelectionConfigurationError: provider is not configured to
                run (including an unsupported reasoning-effort value).
            SchemaSelectionProviderError: the provider call failed, or its
                response body was not valid JSON. May be one of the typed
                subclasses (SchemaSelectionRefusalError,
                SchemaSelectionIncompleteResponseError,
                SchemaSelectionContentFilteredError,
                SchemaSelectionMissingStructuredOutputError) for those
                specific, never-retried outcomes.

        Implementations must NOT catch and swallow any of the above, and
        must NEVER retry internally — the caller (core.sqlservice.
        schema_selection_service) distinguishes these from response-shape
        validation failures and is itself responsible for deciding whether
        (never, on the default path) to try again.
        """
        raise NotImplementedError


class OpenAISchemaSelectionProvider(SchemaSelectionProvider):
    """OpenAI implementation, same client pattern as core.sqlservice.
    sql_generation_providers.OpenAITextToSqlProvider (one synchronous
    OpenAI() client per call, explicit bounded SDK-level max_retries — a
    transport-layer retry for transient network/5xx errors, never an
    application-level re-ask; see this module's own exception docstrings).

    Phase 7, Step 3D: `model`/`reasoning_effort` default to core.config.
    AI_FIRST_SCHEMA_SELECTION_MODEL/AI_FIRST_SCHEMA_SELECTION_REASONING_
    EFFORT — a DEDICATED configuration pair, independent of OPENAI_MODEL
    (used by SQL generation/review/answer generation) and
    OPENAI_EMBEDDING_MODEL. AI_FIRST_SCHEMA_SELECTION_MODEL itself defaults
    to OPENAI_MODEL (see core.config's own comment), so leaving both unset
    reproduces the exact pre-Step-3D default (gpt-4o-mini, no reasoning
    effort). `reasoning_effort` is validated against SUPPORTED_REASONING_
    EFFORTS in the constructor — before any provider call is ever made —
    and, when None (the default), is omitted from the request entirely
    (rather than sent as null), since non-reasoning models reject the
    parameter outright.

    Uses strict JSON Schema Structured Outputs (response_format:
    {"type": "json_schema", "json_schema": {..., "strict": True}}) —
    replacing the prior basic {"type": "json_object"} mode — for every
    model this provider calls; this is a full replacement, not a
    conditional fallback, per Phase 7 Step 3D's own scope (see this
    module's _SCHEMA_SELECTION_RESPONSE_SCHEMA for the exact schema and its
    own docstring for why the deterministic Python validator in
    core.sqlservice.schema_selection_service remains the authoritative
    second boundary regardless). If the selected model or installed SDK
    cannot honor json_schema strict mode, the provider surfaces that as an
    ordinary SchemaSelectionProviderError (via the generic exception
    handler below) — never retried, never silently downgraded to json_object
    mode.
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
        self._model: str = model or AI_FIRST_SCHEMA_SELECTION_MODEL
        self._timeout: int = timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        self._reasoning_effort: str | None = (
            reasoning_effort if reasoning_effort is not None else AI_FIRST_SCHEMA_SELECTION_REASONING_EFFORT
        )
        if self._reasoning_effort is not None and self._reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise SchemaSelectionConfigurationError(
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

    def select_schema(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSchemaSelectionResponse:
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
            # max_completion_tokens, not the deprecated max_tokens: openai's
            # own reasoning-model family (the whole point of Step 3D's
            # evaluation) rejects max_tokens outright with a 400
            # unsupported_parameter error ("Use 'max_completion_tokens'
            # instead") -- confirmed live against gpt-6-astra during this
            # step's own Part 5 availability check. max_completion_tokens is
            # OpenAI's general replacement for max_tokens (accepted by
            # every current chat-completions model, reasoning or not), so
            # this is an unconditional switch, never a model-specific
            # branch.
            max_completion_tokens=max_output_tokens,
            response_format=_RESPONSE_FORMAT,
        )
        if self._reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            # temperature=0 (deterministic structured output) is likewise
            # rejected by reasoning models (they only support the default
            # temperature=1) -- omitted whenever reasoning_effort is
            # configured, since that is this provider's own signal that a
            # reasoning-family model is in use. The default (unconfigured)
            # path keeps temperature=0 exactly as before -- zero behavior
            # change for the existing gpt-4o-mini-class production path.
            create_kwargs["temperature"] = _TEMPERATURE
        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            # Never let an SDK exception's repr leak self._api_key: openai's
            # own exception messages don't include it (only ever sent as an
            # Authorization header), but wrapping here also guarantees this
            # provider never re-raises anything carrying it. Covers
            # model-not-found/permission-denied for an unavailable model —
            # callers distinguish that case, when they need to, by
            # inspecting the wrapped __cause__'s own SDK exception type;
            # this provider itself never special-cases it (no fallback
            # model, no retry, no silent substitution).
            raise SchemaSelectionProviderError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        # Order matters: a refusal can co-occur with empty content, and a
        # truncated response can also leave content empty — check the most
        # specific, most informative condition first so the caller gets the
        # right typed error, never a generic "missing output" catch-all.
        if getattr(message, "refusal", None):
            raise SchemaSelectionRefusalError(
                "the schema-selection provider declined to produce a structured response"
            )
        if finish_reason == "content_filter":
            raise SchemaSelectionContentFilteredError(
                "the schema-selection provider's content-filtering system blocked the response"
            )
        if finish_reason == "length":
            raise SchemaSelectionIncompleteResponseError(
                "the schema-selection provider's response was truncated before completion "
                "(finish_reason='length') -- raise max_output_tokens or reduce the frontier size"
            )

        raw_content = message.content or ""
        if not raw_content.strip():
            raise SchemaSelectionMissingStructuredOutputError(
                "the schema-selection provider returned no structured output content"
            )
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise SchemaSelectionProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderSchemaSelectionResponse(raw=parsed, model=response.model, usage=_safe_usage(response))

    def _assert_available(self) -> None:
        if not self._api_key:
            raise SchemaSelectionConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable schema selection."
            )
        if not _OPENAI_AVAILABLE:
            raise SchemaSelectionConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
