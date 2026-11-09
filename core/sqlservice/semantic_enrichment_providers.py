"""
Provider-neutral semantic-enrichment interface — SQL Architecture
Correction, Phase 6, Step 4A.

`SemanticEnrichmentProvider` is the abstract contract core.sqlservice.
semantic_enrichment_service depends on; it never imports an SDK directly.
`OpenAISemanticEnrichmentProvider` is the one concrete implementation in
this step, following the exact same client pattern already used by
core/ai/providers/openai_provider.py, core/sqlservice/embedding_providers.py,
and core/sqlservice/sql_generation_providers.py: one synchronous OpenAI()
client per call, config read from core.config (falling back to env vars)
unless overridden by constructor arguments, and an explicit, bounded retry
policy (max_retries) rather than a custom retry loop.

This provider is intentionally thin — it sends whatever system/user prompt
strings it is given, parses the response body as JSON, and hands back the
raw parsed object completely unvalidated, exactly like
core.sqlservice.sql_generation_providers.TextToSqlProvider. All prompt
CONSTRUCTION and all STRUCTURED RESPONSE validation (required fields,
types, sizes, referenced-object existence, structural contradictions, ...)
belongs to core.sqlservice.semantic_enrichment_service — this module never
inspects what the JSON actually contains.

Scope: this module only calls the configured LLM and reshapes its raw
response into ProviderSemanticEnrichmentResponse. It never validates
semantics, never persists anything, never connects to a live SQL Server,
and is never called with real schema data outside a test's FakeProvider —
Phase 6 Step 4A makes no real provider call anywhere in this codebase.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# Optional dependency — see core/ai/providers/openai_provider.py's and
# core/sqlservice/embedding_providers.py's identical guard. Keeps this
# module importable in environments where openai isn't installed; only
# OpenAISemanticEnrichmentProvider.enrich_batch() actually needs it.
# ---------------------------------------------------------------------------
try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False

__all__ = [
    "SemanticEnrichmentConfigurationError",
    "SemanticEnrichmentProviderError",
    "ProviderSemanticEnrichmentResponse",
    "SemanticEnrichmentProvider",
    "OpenAISemanticEnrichmentProvider",
]

# Explicit, bounded SDK retry policy (no uncontrolled retry loop) — matches
# the openai-python default exactly, same reasoning as every other OpenAI
# caller in this codebase.
_MAX_SDK_RETRIES = 2

_TEMPERATURE = 0  # deterministic structured output, no sampling variance


class SemanticEnrichmentConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (e.g. no API key, the SDK package not installed) — never a provider-side
    API failure and never a malformed-response validation failure. Also
    reused by core.sqlservice.semantic_enrichment_service for its OWN
    configuration problems (invalid batch-size bounds), exactly like
    core.sqlservice.embedding_coordinator reuses EmbeddingConfigurationError
    from core.sqlservice.embedding_providers for the same purpose."""


class SemanticEnrichmentProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, or a response body that
    isn't valid JSON at all) — wraps whatever the SDK raised. Never raised
    for a configuration problem (see SemanticEnrichmentConfigurationError) or
    a valid-JSON-but-wrong-shape response (see core.sqlservice.
    semantic_enrichment_service.SemanticEnrichmentValidationError)."""


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderSemanticEnrichmentResponse:
    """Result of one SemanticEnrichmentProvider.enrich_batch() call. `raw` is
    the response body parsed as JSON — completely unvalidated;
    core.sqlservice.semantic_enrichment_service is responsible for checking
    its shape before trusting it."""

    raw: Mapping[str, object]
    model: str


# ── Abstract provider interface ───────────────────────────────────────────


class SemanticEnrichmentProvider(ABC):
    """Abstract interface for a structured semantic-enrichment call over one
    bounded batch of structured schema metadata."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable identity, e.g. "openai" — never the model name (see
        `model`) and never an API key or other secret. Part of the cache-key
        identity core.sqlservice.semantic_enrichment_service computes (see
        SemanticEnrichmentCacheKey) — a single source of truth on the
        provider instance itself."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        """The specific chat/completion model this provider calls."""
        raise NotImplementedError

    @abstractmethod
    def enrich_batch(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSemanticEnrichmentResponse:
        """
        Send one system/user prompt pair (one bounded batch of structured
        schema metadata) and return the parsed JSON body.

        Raises:
            SemanticEnrichmentConfigurationError: provider is not configured
                to run.
            SemanticEnrichmentProviderError: the provider call failed, or
                its response body was not valid JSON.

        Implementations must NOT catch and swallow either — the caller
        (core.sqlservice.semantic_enrichment_service) distinguishes them
        from response-shape validation failures.
        """
        raise NotImplementedError


# ── OpenAI implementation ──────────────────────────────────────────────────


class OpenAISemanticEnrichmentProvider(SemanticEnrichmentProvider):
    """
    OpenAI implementation of SemanticEnrichmentProvider, compatible with the
    openai==2.38.0 SDK already declared in requirements.txt
    (client.chat.completions.create(..., response_format={"type":
    "json_object"}) -> a response whose choices[0].message.content is a
    JSON string — the exact same pattern already used by
    core.sqlservice.sql_generation_providers.OpenAITextToSqlProvider and
    every other OpenAI JSON-mode caller in this codebase).

    Config is read from core.config (OPENAI_API_KEY, OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS) at instantiation time — the SAME chat-model
    variables the rest of ToolSmithAI's OpenAI callers use; no dedicated
    semantic-enrichment model/credential variable is introduced. Constructor
    parameters override them — use them in tests (with a fake api_key;
    enrich_batch() is never actually called by this project's tests —
    tests use fake providers only, per Phase 6 Step 4A's restrictions).
    """

    _PROVIDER_ID = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._api_key: str = api_key if api_key is not None else OPENAI_API_KEY
        self._model: str = model or OPENAI_MODEL
        self._timeout: int = (
            timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        )

    # ── SemanticEnrichmentProvider implementation ─────────────────────────

    @property
    def provider_id(self) -> str:
        return self._PROVIDER_ID

    @property
    def model(self) -> str:
        return self._model

    def enrich_batch(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSemanticEnrichmentResponse:
        self._assert_available()

        client = _openai.OpenAI(
            api_key=self._api_key,
            timeout=timeout_seconds if timeout_seconds is not None else self._timeout,
            max_retries=_MAX_SDK_RETRIES,
        )
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_output_tokens,
                temperature=_TEMPERATURE,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            # Never let an SDK exception's repr leak self._api_key: openai's
            # own exception messages don't include it (only ever sent as an
            # Authorization header), but wrapping here also guarantees this
            # provider never re-raises anything carrying it.
            raise SemanticEnrichmentProviderError(f"{type(exc).__name__}: {exc}") from exc

        raw_content = response.choices[0].message.content or ""
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise SemanticEnrichmentProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderSemanticEnrichmentResponse(raw=parsed, model=response.model)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _assert_available(self) -> None:
        if not self._api_key:
            raise SemanticEnrichmentConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable semantic enrichment."
            )
        if not _OPENAI_AVAILABLE:
            raise SemanticEnrichmentConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
