from __future__ import annotations

import json
import logging
import os

from ..models import AISemanticContext, AISemanticResult
from ..prompt_builder import PromptBuilder
from ..semantic_intelligence import AISemanticProvider, SemanticIntelligenceService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency — openai is installed in this project (requirements.txt),
# but we guard the import so the module can be imported in environments where
# the package is absent (e.g. a stripped test image) without crashing at
# import time.  The guard is module-level so tests can patch it directly.
# ---------------------------------------------------------------------------
try:
    import openai as _openai  # type: ignore[import-untyped]
    _OPENAI_AVAILABLE = True
except ImportError:
    _openai = None  # type: ignore[assignment]
    _OPENAI_AVAILABLE = False


class OpenAISemanticProvider(AISemanticProvider):
    """
    OpenAI implementation of AISemanticProvider.

    Follows the same client pattern used by the rest of ToolSmithAI
    (task_interpreter, intent_composer, report_generator, routes) — one
    synchronous OpenAI() client per call, JSON mode always on, temperature=0
    for deterministic classification.

    Config is read from environment variables at instantiation time.
    Constructor parameters override env vars — use them in tests.

    Failures raise exceptions; SemanticIntelligenceService.analyze() catches
    all of them and returns None so the caller always has a safe fallback.
    """

    # Generous but bounded — the response schema is compact (≈200–350 tokens).
    _MAX_TOKENS: int = 800
    _TEMPERATURE: int = 0

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        # Prefer injected values; fall back to the same env vars used by the
        # existing OpenAI callers (OPENAI_API_KEY, OPENAI_MODEL) plus the
        # Phase 3A-specific timeout (AI_SEMANTIC_TIMEOUT_SECONDS).
        self._api_key: str = (
            api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")
        )
        self._model: str = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._timeout: int = (
            timeout_seconds
            if timeout_seconds is not None
            else int(os.getenv("AI_SEMANTIC_TIMEOUT_SECONDS", "15"))
        )
        self._prompt_builder = PromptBuilder()

    # ── AISemanticProvider implementation ────────────────────────────────────

    def analyze_metadata(self, context: AISemanticContext) -> AISemanticResult:
        """
        Call OpenAI to classify the column described by *context*.

        Raises:
            RuntimeError: OPENAI_API_KEY is not set, or openai package missing.
            json.JSONDecodeError: response body is not valid JSON.
            ValueError: response JSON passes parsing but fails schema validation
                        (missing keys, confidence out of range, empty reasoning).
            openai.OpenAIError (or subclass): the API call itself failed
                        (rate-limit, auth, timeout, server error, etc.).

        All of the above are caught by SemanticIntelligenceService.analyze()
        which returns None instead of propagating them to the caller.
        """
        self._assert_available()

        system_prompt = self._prompt_builder.get_system_prompt()
        user_prompt = self._prompt_builder.build_metadata_analysis_prompt(context)

        client = _openai.OpenAI(api_key=self._api_key, timeout=self._timeout)

        logger.debug(
            "Calling OpenAI %s for %s.%s (max_tokens=%d)",
            self._model,
            context.table_fqn,
            context.column_name,
            self._MAX_TOKENS,
        )

        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=self._MAX_TOKENS,
            temperature=self._TEMPERATURE,
            response_format={"type": "json_object"},
        )

        raw_content = response.choices[0].message.content or ""
        raw: dict = json.loads(raw_content)
        return SemanticIntelligenceService.validate_result_json(raw)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _assert_available(self) -> None:
        """Raise RuntimeError for known unavailable conditions before any API call."""
        if not self._api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not configured. "
                "Set it in .env to enable AI semantic intelligence."
            )
        if not _OPENAI_AVAILABLE:
            raise RuntimeError(
                "openai package is not installed. "
                "Install it with: pip install 'openai>=2.0,<3.0'"
            )
