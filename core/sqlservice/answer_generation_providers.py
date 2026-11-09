"""
Provider-neutral answer-generation interface — SQL rebuild Phase 3, Step 4.

`AnswerGenerationProvider` is the abstract contract core.sqlservice.
answer_generation_service depends on; it never imports an SDK directly.
`OpenAIAnswerGenerationProvider` is the one concrete implementation in this
step, following the exact same client pattern already used by
core/sqlservice/sql_generation_providers.py (and, before that, core/ai/
providers/openai_provider.py and core/sqlservice/embedding_providers.py):
one synchronous OpenAI() client per call, config read from core.config
(falling back to env vars) unless overridden by constructor arguments, and
an explicit, bounded retry policy (max_retries) rather than a custom retry
loop. No dedicated answer-generation model/credential variable is
introduced — this reuses OPENAI_API_KEY/OPENAI_MODEL/OPENAI_TIMEOUT_SECONDS,
the same chat-model configuration every other OpenAI caller in this project
uses.

This provider is intentionally thin: it sends whatever system/user prompt
strings it is given, parses the response body as JSON, and hands back the
raw parsed object completely unvalidated. All prompt CONSTRUCTION and all
STRUCTURED RESPONSE validation/grounding belongs to core.sqlservice.
answer_generation_service — this module never inspects what the JSON
actually contains.

Scope: this module only calls the configured LLM and reshapes its raw
response into ProviderAnswerGenerationResponse. It never builds a prompt,
never validates or grounds an answer, and never executes SQL.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from core.config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT_SECONDS

# ---------------------------------------------------------------------------
# Optional dependency — see core/sqlservice/sql_generation_providers.py's
# identical guard. Keeps this module importable in environments where
# openai isn't installed; only OpenAIAnswerGenerationProvider.generate_answer()
# actually needs it.
# ---------------------------------------------------------------------------
try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


# Explicit, bounded SDK retry policy (no uncontrolled retry loop) — matches
# core/sqlservice/sql_generation_providers.py's own choice, same reasoning.
_MAX_SDK_RETRIES = 2

_TEMPERATURE = 0  # deterministic structured output, no sampling variance


class AnswerGenerationConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (e.g. no API key, the SDK package not installed) — never a provider-side
    API failure and never a malformed-response validation failure."""


class AnswerGenerationProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, or a response body that
    isn't valid JSON at all) — wraps whatever the SDK raised. Never raised
    for a configuration problem (see AnswerGenerationConfigurationError) or
    a valid-JSON-but-wrong-shape/ungrounded response (see core.sqlservice.
    answer_generation_service.AnswerValidationError)."""


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderAnswerGenerationResponse:
    """Result of one AnswerGenerationProvider.generate_answer() call. `raw`
    is the response body parsed as JSON — completely unvalidated;
    core.sqlservice.answer_generation_service is responsible for checking
    its shape and grounding every insight before trusting it."""

    raw: Mapping[str, object]
    model: str


# ── Abstract provider interface ───────────────────────────────────────────


class AnswerGenerationProvider(ABC):
    """Abstract interface for a structured answer-generation call."""

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Stable identity, e.g. "openai" — never the model name (see
        `model`) and never an API key or other secret."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        """The specific chat/completion model this provider calls."""
        raise NotImplementedError

    @abstractmethod
    def generate_answer(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderAnswerGenerationResponse:
        """
        Send one system/user prompt pair and return the parsed JSON body.

        Raises:
            AnswerGenerationConfigurationError: provider is not configured
                to run.
            AnswerGenerationProviderError: the provider call failed, or its
                response body was not valid JSON.

        Implementations must NOT catch and swallow either — the caller
        (core.sqlservice.answer_generation_service) distinguishes them from
        response-shape/grounding validation failures.
        """
        raise NotImplementedError


# ── OpenAI implementation ──────────────────────────────────────────────────


class OpenAIAnswerGenerationProvider(AnswerGenerationProvider):
    """
    OpenAI implementation of AnswerGenerationProvider, compatible with the
    openai==2.38.0 SDK already declared in requirements.txt
    (client.chat.completions.create(..., response_format={"type":
    "json_object"}) -> a response whose choices[0].message.content is a
    JSON string — the exact same pattern already used by
    core/sqlservice/sql_generation_providers.py and every other OpenAI
    JSON-mode caller in this codebase).

    Config is read from core.config (OPENAI_API_KEY, OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS) at instantiation time — the SAME chat-model
    variables the rest of ToolSmithAI's OpenAI callers use; no dedicated
    answer-generation model/credential variable is introduced. Constructor
    parameters override them — use them in tests (with a fake api_key;
    generate_answer() is never actually called by this project's tests).
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

    # ── AnswerGenerationProvider implementation ────────────────────────────

    @property
    def provider_id(self) -> str:
        return self._PROVIDER_ID

    @property
    def model(self) -> str:
        return self._model

    def generate_answer(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderAnswerGenerationResponse:
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
            # Authorization header). core.sqlservice.answer_generation_service
            # additionally never forwards this text to a caller anyway — it
            # is caught there and replaced with a fixed, category-derived
            # message (defense in depth: this project's SQL-execution step
            # already established that no caught exception's raw text is
            # ever trusted, since some failure sources genuinely can embed a
            # credential — this provider's own text is believed safe, but is
            # still never relied upon downstream).
            raise AnswerGenerationProviderError(f"{type(exc).__name__}: {exc}") from exc

        raw_content = response.choices[0].message.content or ""
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise AnswerGenerationProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderAnswerGenerationResponse(raw=parsed, model=response.model)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _assert_available(self) -> None:
        if not self._api_key:
            raise AnswerGenerationConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable answer generation."
            )
        if not _OPENAI_AVAILABLE:
            raise AnswerGenerationConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
