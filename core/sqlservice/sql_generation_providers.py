"""
Provider-neutral text-to-SQL interface — SQL rebuild Phase 3, Step 1.

`TextToSqlProvider` is the abstract contract core.sqlservice.
sql_generation_service depends on; it never imports an SDK directly.
`OpenAITextToSqlProvider` is the one concrete implementation in this step,
following the same client pattern already used by core/ai/providers/
openai_provider.py and core/sqlservice/embedding_providers.py: one
synchronous OpenAI() client per call, config read from core.config
(falling back to env vars) unless overridden by constructor arguments, and
an explicit, bounded retry policy (max_retries) rather than a custom retry
loop.

This provider is intentionally thin: it sends whatever system/user prompt
strings it is given, parses the response body as JSON, and hands back the
raw parsed object completely unvalidated. All prompt CONSTRUCTION and all
STRUCTURED RESPONSE validation (required fields, types, sizes, referenced-
object existence, ...) belongs to core.sqlservice.sql_generation_service —
this module never inspects what the JSON actually contains.

Scope: this module only calls the configured LLM and reshapes its raw
response into ProviderSqlGenerationResponse. It never validates SQL
safety, never executes anything, and never decides what schema context or
question text to send.
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
# OpenAITextToSqlProvider.generate_sql() actually needs it.
# ---------------------------------------------------------------------------
try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


# Explicit, bounded SDK retry policy (no uncontrolled retry loop) — matches
# the openai-python default exactly, but stated here rather than left
# implicit, same reasoning as core/sqlservice/embedding_providers.py.
_MAX_SDK_RETRIES = 2

_TEMPERATURE = 0  # deterministic structured output, no sampling variance

# Phase 7, Step 4 correction (2026-09-28) — same reasoning-family support
# core.sqlservice.schema_selection_providers.OpenAISchemaSelectionProvider
# already has (see that module's own docstring/constant of the same name).
# Live Gate C run discovered this provider had no way to run a reasoning-
# family model (e.g. gpt-6-sol) at all: generate_sql() always sent the
# deprecated max_tokens (reasoning models reject it outright with a 400) and
# never sent `reasoning_effort`. The SQL-review stage's mandatory intent-
# evidence contract (the retired SQL-review stage) needs a model in
# the SAME already-proven capable tier Step 3D/3H/3K already validated for
# schema selection's comparably demanding structured-output contract — not
# a new model comparison, the identical established choice applied to this
# provider too.
SUPPORTED_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})


class TextToSqlConfigurationError(RuntimeError):
    """Raised when a provider cannot run due to missing/invalid configuration
    (e.g. no API key, the SDK package not installed) — never a provider-side
    API failure and never a malformed-response validation failure."""


class TextToSqlProviderError(RuntimeError):
    """Raised when the underlying provider call itself failed (network
    error, rate limit, timeout, non-2xx response, or a response body that
    isn't valid JSON at all) — wraps whatever the SDK raised. Never raised
    for a configuration problem (see TextToSqlConfigurationError) or a
    valid-JSON-but-wrong-shape response (see core.sqlservice.
    sql_generation_service.TextToSqlValidationError)."""


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderSqlGenerationResponse:
    """Result of one TextToSqlProvider.generate_sql() call. `raw` is the
    response body parsed as JSON — completely unvalidated; core.sqlservice.
    sql_generation_service is responsible for checking its shape before
    trusting it."""

    raw: Mapping[str, object]
    model: str


# ── Abstract provider interface ───────────────────────────────────────────


class TextToSqlProvider(ABC):
    """Abstract interface for a structured text-to-SQL generation call."""

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
    def generate_sql(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSqlGenerationResponse:
        """
        Send one system/user prompt pair and return the parsed JSON body.

        Raises:
            TextToSqlConfigurationError: provider is not configured to run.
            TextToSqlProviderError: the provider call failed, or its
                response body was not valid JSON.

        Implementations must NOT catch and swallow either — the caller
        (core.sqlservice.sql_generation_service) distinguishes them from
        response-shape validation failures.
        """
        raise NotImplementedError


# ── OpenAI implementation ──────────────────────────────────────────────────


class OpenAITextToSqlProvider(TextToSqlProvider):
    """
    OpenAI implementation of TextToSqlProvider, compatible with the
    openai==2.38.0 SDK already declared in requirements.txt
    (client.chat.completions.create(..., response_format={"type":
    "json_object"}) -> a response whose choices[0].message.content is a
    JSON string — the exact same pattern already used by
    core/ai/providers/openai_provider.py and every other OpenAI JSON-mode
    caller in this codebase).

    Config is read from core.config (OPENAI_API_KEY, OPENAI_MODEL,
    OPENAI_TIMEOUT_SECONDS) at instantiation time — the SAME chat-model
    variables the rest of ToolSmithAI's OpenAI callers use; no dedicated
    SQL-generation model/credential variable is introduced. Constructor
    parameters override them — use them in tests (with a fake api_key;
    generate_sql() is never actually called by this project's tests).

    `reasoning_effort` (Phase 7, Step 4 correction, default None — zero
    behavior change for every existing caller, which never passes it):
    when set, generate_sql() sends `reasoning_effort` and omits
    `temperature` (reasoning models only support their default
    temperature=1 — mirrors core.sqlservice.schema_selection_providers.
    OpenAISchemaSelectionProvider's own identical reasoning-model handling).
    Validated against SUPPORTED_REASONING_EFFORTS in the constructor, before
    any provider call is ever made.
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
        self._model: str = model or OPENAI_MODEL
        self._timeout: int = (
            timeout_seconds if timeout_seconds is not None else OPENAI_TIMEOUT_SECONDS
        )
        self._reasoning_effort: str | None = reasoning_effort
        if self._reasoning_effort is not None and self._reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise TextToSqlConfigurationError(
                f"unsupported reasoning_effort {self._reasoning_effort!r}; must be one of "
                f"{sorted(SUPPORTED_REASONING_EFFORTS)} or None"
            )

    # ── TextToSqlProvider implementation ──────────────────────────────────

    @property
    def provider_id(self) -> str:
        return self._PROVIDER_ID

    @property
    def model(self) -> str:
        return self._model

    @property
    def reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    def generate_sql(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        timeout_seconds: int | None = None,
    ) -> ProviderSqlGenerationResponse:
        self._assert_available()

        client = _openai.OpenAI(
            api_key=self._api_key,
            timeout=timeout_seconds if timeout_seconds is not None else self._timeout,
            max_retries=_MAX_SDK_RETRIES,
        )
        # max_completion_tokens, not the deprecated max_tokens: a reasoning-
        # family model rejects max_tokens outright with a 400
        # unsupported_parameter error — same fix, same reasoning, as
        # OpenAISchemaSelectionProvider's own identical switch.
        # max_completion_tokens is accepted by every current chat-
        # completions model, reasoning or not, so this is unconditional,
        # not a model-specific branch.
        create_kwargs: dict = dict(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=max_output_tokens,
            response_format={"type": "json_object"},
        )
        if self._reasoning_effort is not None:
            create_kwargs["reasoning_effort"] = self._reasoning_effort
        else:
            # Zero behavior change for the existing gpt-4o-mini-class
            # production path: temperature=0 only when no reasoning_effort
            # is configured (reasoning models reject a non-default
            # temperature).
            create_kwargs["temperature"] = _TEMPERATURE
        try:
            response = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            # Never let an SDK exception's repr leak self._api_key: openai's
            # own exception messages don't include it (only ever sent as an
            # Authorization header), but wrapping here also guarantees this
            # provider never re-raises anything carrying it.
            raise TextToSqlProviderError(f"{type(exc).__name__}: {exc}") from exc

        raw_content = response.choices[0].message.content or ""
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError) as exc:
            raise TextToSqlProviderError(f"provider response was not valid JSON: {exc}") from exc

        return ProviderSqlGenerationResponse(raw=parsed, model=response.model)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _assert_available(self) -> None:
        if not self._api_key:
            raise TextToSqlConfigurationError(
                "OPENAI_API_KEY is not configured. Set it in .env to enable SQL generation."
            )
        if not _OPENAI_AVAILABLE:
            raise TextToSqlConfigurationError(
                "openai package is not installed. Install it with: pip install 'openai>=2.0,<3.0'"
            )
