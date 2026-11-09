"""
Provider-neutral AI business-knowledge-mapping interface — Phase 7, Step 3J,
Part 4.

Structurally identical to core.sqlservice.schema_selection_providers (same
exception categories, same frozen unvalidated-raw response shape, same ABC
contract, same OpenAI client pattern, same strict JSON Schema Structured
Outputs approach) — deliberately NOT a shared base class, per this
codebase's established "one small, independent provider module per AI call
type" convention (see schema_selection_providers's own module docstring).

This provider is intentionally thin: it sends a system/user prompt pair
(built by core.sqlservice.business_knowledge_mapper from ALREADY-persisted,
already-bounded package concepts and schema-document facts — never a raw
question, never row data) and hands back the raw parsed JSON completely
unvalidated. All response-shape and structural-fact validation belongs to
core.sqlservice.business_knowledge_mapper, which never trusts anything this
module returns without independently checking it against the real schema
(object existence, tier eligibility, real columns, authority/safety class) —
see that module's own docstring for the full deterministic-verification
boundary.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.config import (
    BUSINESS_KNOWLEDGE_MAPPING_MODEL,
    BUSINESS_KNOWLEDGE_MAPPING_REASONING_EFFORT,
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

SUPPORTED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

# One proposed mapping. Field names/shape mirror
# data.models.ensure_business_knowledge_tables' own business_knowledge_
# mappings columns exactly (see that function's docstring) -- this schema is
# an OpenAI Structured-Outputs-specific artifact of the SAME contract, kept
# in sync by hand (this file's own convention, matching schema_selection_
# providers' identical choice).
_PROPOSED_MAPPING_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "concept_id", "object_fqn", "columns", "mapping_role", "mapping_kind",
        "confidence", "evidence",
    ],
    "properties": {
        "concept_id": {"type": "string"},
        "object_fqn": {"type": "string"},
        "columns": {"type": "array", "items": {"type": "string"}},
        "mapping_role": {
            "type": "string",
            # Phase 7, Step 3J1 -- deliberately NOT offering FEE_DEFINITION_
            # SOURCE/FEE_PAYMENT_SOURCE here yet, even though data.
            # business_knowledge_mapping_store.MAPPING_ROLES (the Python
            # validation vocabulary) and a fresh business_knowledge_mappings
            # table (data.models.ensure_business_knowledge_tables) both
            # already accept them: an ALREADY-DEPLOYED table created before
            # this change still has the NARROWER original CHECK constraint
            # (SQLite cannot widen one without a table rebuild -- an
            # explicit, separate, per-source migration decision, never done
            # implicitly here). Offering these roles to the model now would
            # let it legitimately choose one for a source whose live table
            # cannot yet store it, silently losing that proposal to
            # run_business_knowledge_mapping's own storage-safety fallback
            # instead of it landing under an already-storable role. Add
            # them to this enum only once every source this ships against
            # is confirmed to have the widened constraint.
            "enum": [
                "CANONICAL_ENTITY_SOURCE", "PREFERRED_BUSINESS_VIEW", "DETAIL_ACTIVITY_SOURCE",
                "ENROLLMENT_SOURCE", "FEE_SOURCE", "FULL_PAYMENT_SOURCE", "INSTALLMENT_PAYMENT_SOURCE",
                "STATUS_SOURCE", "OUTCOME_SOURCE", "CONNECTOR_ONLY", "UNSUPPORTED_OR_CONFLICTING",
            ],
        },
        "mapping_kind": {
            "type": "string",
            "enum": ["DIRECT", "DEPENDENT", "EQUIVALENT", "CONDITIONAL", "UNRESOLVED"],
        },
        "confidence": {"type": "number"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}

_MAPPING_PROPOSAL_RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["mappings"],
    "properties": {
        "mappings": {"type": "array", "items": _PROPOSED_MAPPING_SCHEMA},
    },
}

_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "business_knowledge_mapping_proposal",
        "strict": True,
        "schema": _MAPPING_PROPOSAL_RESPONSE_SCHEMA,
    },
}

__all__ = [
    "SUPPORTED_REASONING_EFFORTS",
    "BusinessKnowledgeMappingConfigurationError",
    "BusinessKnowledgeMappingProviderError",
    "BusinessKnowledgeMappingRefusalError",
    "BusinessKnowledgeMappingIncompleteResponseError",
    "BusinessKnowledgeMappingContentFilteredError",
    "BusinessKnowledgeMappingMissingStructuredOutputError",
    "ProviderMappingProposalResponse",
    "BusinessKnowledgeMappingProvider",
    "OpenAIBusinessKnowledgeMappingProvider",
]


class BusinessKnowledgeMappingConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (no API key, SDK not installed, unsupported reasoning_effort) — always
    raised before any provider call is attempted."""


class BusinessKnowledgeMappingProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx, or non-JSON body) — wraps whatever
    the SDK raised. The four subclasses below narrow this to a specific,
    sanitized, never-retried outcome."""


class BusinessKnowledgeMappingRefusalError(BusinessKnowledgeMappingProviderError):
    """The model declined to respond at all (Structured Outputs' own
    safety-refusal channel). Sanitized: never carries the refusal text."""


class BusinessKnowledgeMappingIncompleteResponseError(BusinessKnowledgeMappingProviderError):
    """The response was cut off before completion (finish_reason ==
    'length'). Sanitized: never carries the truncated partial content.
    Never retried -- the caller must raise max_output_tokens or shrink the
    batch, not silently re-ask."""


class BusinessKnowledgeMappingContentFilteredError(BusinessKnowledgeMappingProviderError):
    """The provider's own content-filtering system blocked the response."""


class BusinessKnowledgeMappingMissingStructuredOutputError(BusinessKnowledgeMappingProviderError):
    """A normal (non-refused) response with no usable message content."""


def _safe_usage(response: object) -> Mapping[str, int] | None:
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
class ProviderMappingProposalResponse:
    """Result of one BusinessKnowledgeMappingProvider.propose_mappings()
    call. `raw` is the parsed JSON body, completely unvalidated --
    core.sqlservice.business_knowledge_mapper is responsible for checking
    its shape and every structural fact before trusting any of it."""

    raw: Mapping[str, object]
    model: str
    usage: Mapping[str, int] | None = None


class BusinessKnowledgeMappingProvider(ABC):
    """Abstract interface for a structured AI mapping-proposal call."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def propose_mappings(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderMappingProposalResponse:
        """
        Raises:
            BusinessKnowledgeMappingConfigurationError: provider not
                configured to run.
            BusinessKnowledgeMappingProviderError: the call failed, or the
                response body was not valid JSON. May be one of the typed
                subclasses above for those specific, never-retried outcomes.

        Implementations must NOT catch and swallow any of the above, and
        must NEVER retry internally.
        """
        raise NotImplementedError


class OpenAIBusinessKnowledgeMappingProvider(BusinessKnowledgeMappingProvider):
    """OpenAI implementation -- same client pattern as core.sqlservice.
    schema_selection_providers.OpenAISchemaSelectionProvider (one synchronous
    OpenAI() client per call, explicit bounded SDK-level max_retries, strict
    JSON Schema Structured Outputs). model/reasoning_effort default to core.
    config.BUSINESS_KNOWLEDGE_MAPPING_MODEL/_REASONING_EFFORT -- a dedicated
    pair, independent of OPENAI_MODEL/AI_FIRST_SCHEMA_SELECTION_MODEL."""

    _PROVIDER_ID = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._api_key: str = api_key if api_key is not None else OPENAI_API_KEY
        self._model: str = model or BUSINESS_KNOWLEDGE_MAPPING_MODEL
        self._timeout: int = timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        self._reasoning_effort: str | None = (
            reasoning_effort if reasoning_effort is not None else BUSINESS_KNOWLEDGE_MAPPING_REASONING_EFFORT
        )
        if self._reasoning_effort is not None and self._reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise BusinessKnowledgeMappingConfigurationError(
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

    def propose_mappings(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderMappingProposalResponse:
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
            create_kwargs["temperature"] = _TEMPERATURE
        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            raise BusinessKnowledgeMappingProviderError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason

        if getattr(message, "refusal", None):
            raise BusinessKnowledgeMappingRefusalError(
                "the business-knowledge mapping provider declined to produce a structured response"
            )
        if finish_reason == "content_filter":
            raise BusinessKnowledgeMappingContentFilteredError(
                "the business-knowledge mapping provider's content-filtering system blocked the response"
            )
        if finish_reason == "length":
            raise BusinessKnowledgeMappingIncompleteResponseError(
                "the business-knowledge mapping provider's response was truncated before completion "
                "(finish_reason='length') -- raise max_output_tokens or shrink the batch"
            )

        raw_content = message.content or ""
        if not raw_content.strip():
            raise BusinessKnowledgeMappingMissingStructuredOutputError(
                "the business-knowledge mapping provider returned no structured output content"
            )
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise BusinessKnowledgeMappingProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderMappingProposalResponse(raw=parsed, model=response.model, usage=_safe_usage(response))

    def _assert_available(self) -> None:
        if not self._api_key:
            raise BusinessKnowledgeMappingConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable business-knowledge mapping."
            )
        if not _OPENAI_AVAILABLE:
            raise BusinessKnowledgeMappingConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
