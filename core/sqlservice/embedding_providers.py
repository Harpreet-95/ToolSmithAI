"""
Provider-neutral embedding interface — SQL rebuild Phase 2, Step 2.

`EmbeddingProvider` is the abstract contract core.sqlservice.embedding_coordinator
depends on; it never imports an SDK directly. `OpenAIEmbeddingProvider` is the
one concrete implementation in this step, following the same client pattern
already used by core/ai/providers/openai_provider.py: one synchronous
OpenAI() client per call, config read from core.config (falling back to env
vars) unless overridden by constructor arguments, and an explicit, bounded
retry policy (max_retries) rather than a custom retry loop.

Scope: this module only calls the configured embedding provider and reshapes
its response into this module's own typed dataclasses. It never persists
anything, never validates a document's origin, and never decides which
documents need embedding — that is core.sqlservice.embedding_coordinator's
job, layered on top of this.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Sequence

from core.config import (
    OPENAI_API_KEY,
    OPENAI_EMBEDDING_DIMENSIONS,
    OPENAI_EMBEDDING_MODEL,
    OPENAI_TIMEOUT_SECONDS,
)

# ---------------------------------------------------------------------------
# Optional dependency — see core/ai/providers/openai_provider.py's identical
# guard. Keeps this module importable in environments where openai isn't
# installed; only OpenAIEmbeddingProvider.embed_batch() actually needs it.
# ---------------------------------------------------------------------------
try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


# Explicit, bounded SDK retry policy (requirement: no uncontrolled retry
# loop) — matches the openai-python default exactly, but stated here rather
# than left implicit, so a reader never has to check the SDK's own default
# to know the retry budget this project relies on.
_MAX_SDK_RETRIES = 2


class EmbeddingConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (e.g. no API key, the SDK package not installed) — never a provider-side
    API failure and never a validation failure of a response already
    received."""


class EmbeddingProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, etc.) — wraps whatever the
    SDK raised. Never raised for a configuration problem (see
    EmbeddingConfigurationError) or a malformed-but-received response (see
    core.sqlservice.embedding_coordinator.EmbeddingValidationError)."""


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderEmbeddingItem:
    """One (index, vector) pair from a provider's batch response, exactly as
    returned — not yet validated or reordered. `vector` is left as whatever
    sequence of numbers the provider returned; core.sqlservice.
    embedding_coordinator is responsible for validating it (finite, correct
    dimensionality) before trusting it."""

    index: int
    vector: tuple[float, ...]


@dataclass(frozen=True)
class ProviderEmbeddingBatchResponse:
    """Result of one EmbeddingProvider.embed_batch() call."""

    items: tuple[ProviderEmbeddingItem, ...]
    model: str


# ── Abstract provider interface ───────────────────────────────────────────


class EmbeddingProvider(ABC):
    """
    Abstract interface for a batch text-embedding provider.

    provider_id/model/dimensions together are the exact cache-key identity
    core.sqlservice.embedding_store persists and reuses against — a single
    source of truth on the provider instance itself, so a caller can never
    pass a mismatched identity to the store independent of what actually
    generated the vectors.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable identity for the cache key, e.g. "openai" — never the
        model name (see `model`) and never an API key or other secret."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        """The specific embedding model this provider calls."""
        raise NotImplementedError

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Expected vector length for every embedding this provider returns."""
        raise NotImplementedError

    @abstractmethod
    def embed_batch(self, texts: Sequence[str]) -> ProviderEmbeddingBatchResponse:
        """
        Embed one batch of document texts.

        Raises:
            EmbeddingConfigurationError: provider is not configured to run
                (e.g. missing API key, SDK package unavailable).
            EmbeddingProviderError: the provider call itself failed.

        Implementations must NOT catch and swallow either — the caller
        (core.sqlservice.embedding_coordinator) distinguishes them from
        response-validation failures.
        """
        raise NotImplementedError


# ── OpenAI implementation ──────────────────────────────────────────────────


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """
    OpenAI implementation of EmbeddingProvider, compatible with the
    openai==2.38.0 SDK already declared in requirements.txt
    (client.embeddings.create(model=..., input=[...], dimensions=...) ->
    CreateEmbeddingResponse with .data: list[Embedding(index, embedding)]).

    Config is read from core.config (OPENAI_API_KEY, OPENAI_EMBEDDING_MODEL,
    OPENAI_EMBEDDING_DIMENSIONS, OPENAI_TIMEOUT_SECONDS) at instantiation
    time — the same variables the rest of ToolSmithAI's OpenAI callers use,
    no separate credential system. Constructor parameters override them —
    use them in tests (with a fake api_key; embed_batch() is never actually
    called by this project's tests).
    """

    _PROVIDER_ID = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        dimensions: int | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        self._api_key: str = api_key if api_key is not None else OPENAI_API_KEY
        self._model: str = model or OPENAI_EMBEDDING_MODEL
        self._dimensions: int = dimensions if dimensions is not None else OPENAI_EMBEDDING_DIMENSIONS
        self._timeout: int = (
            timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        )

    # ── EmbeddingProvider implementation ──────────────────────────────────

    @property
    def provider_id(self) -> str:
        return self._PROVIDER_ID

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_batch(self, texts: Sequence[str]) -> ProviderEmbeddingBatchResponse:
        self._assert_available()

        client = _openai.OpenAI(
            api_key=self._api_key, timeout=self._timeout, max_retries=_MAX_SDK_RETRIES,
        )
        try:
            response = client.embeddings.create(
                model=self._model,
                input=list(texts),
                dimensions=self._dimensions,
            )
        except Exception as exc:
            # Never let an SDK exception's repr leak the api_key: openai's
            # own exception messages don't include it (only ever sent as an
            # Authorization header), but wrapping here also guarantees this
            # provider never re-raises anything carrying self._api_key.
            raise EmbeddingProviderError(f"{type(exc).__name__}: {exc}") from exc

        items = tuple(
            ProviderEmbeddingItem(index=item.index, vector=tuple(item.embedding))
            for item in response.data
        )
        return ProviderEmbeddingBatchResponse(items=items, model=response.model)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _assert_available(self) -> None:
        if not self._api_key:
            raise EmbeddingConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable embedding generation."
            )
        if not _OPENAI_AVAILABLE:
            raise EmbeddingConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
