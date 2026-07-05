from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Any

from .models import AISemanticContext, AISemanticResult
from .prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

# Read once at import time; can be overridden per-instance for tests.
_DEFAULT_CONFIDENCE_THRESHOLD: float = float(
    os.getenv("AI_CONFIDENCE_THRESHOLD", "0.75")
)


# ---------------------------------------------------------------------------
# Abstract provider interface
# ---------------------------------------------------------------------------


class AISemanticProvider(ABC):
    """
    Abstract interface for AI-powered metadata classification.

    Concrete implementations (e.g. OpenAISemanticProvider, AnthropicSemanticProvider)
    must subclass this and implement analyze_metadata().

    CONTRACT:
    - analyze_metadata() must return a valid AISemanticResult.
    - Implementations must NOT query the database — all context is pre-loaded.
    - Implementations must NOT auto-approve results; review_required is set by
      the provider but the decision is made by a human reviewer.
    - This method is only called when rule-engine confidence is below the
      configured threshold.  SemanticIntelligenceService enforces that invariant.
    """

    @abstractmethod
    def analyze_metadata(self, context: AISemanticContext) -> AISemanticResult:
        """
        Classify the column described by *context* and return a structured result.

        Raises:
            NotImplementedError: if the subclass has not provided an implementation.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has not implemented analyze_metadata(). "
            "Register a concrete provider before use."
        )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


class SemanticIntelligenceService:
    """
    Orchestrates the two-stage metadata intelligence pipeline:

        Stage 1 — Rule engine runs first (always; handled by caller).
        Stage 2 — If rule-engine confidence < threshold, call AI provider.

    The service never runs AI first, never auto-approves AI results, and
    never modifies rule-engine logic.  It is a read-only analysis layer
    that supplements uncertain classifications.

    Usage example (callers are responsible for stage 1):

        svc = SemanticIntelligenceService(provider=my_provider)

        domain_result = detect_table_domain(table_profile)          # stage 1

        if svc.should_invoke_ai(domain_result.confidence):
            ctx = build_context(table_profile, domain_result, ...)  # caller builds
            ai_result = svc.analyze(ctx)                            # stage 2
            # ai_result.review_required is always checked before use
    """

    def __init__(
        self,
        provider: AISemanticProvider | None = None,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._provider = provider
        self._threshold = confidence_threshold
        self._prompt_builder = PromptBuilder()

    # ── Configuration ────────────────────────────────────────────────────────

    @property
    def confidence_threshold(self) -> float:
        """Rule-engine confidence below which AI analysis is triggered."""
        return self._threshold

    # ── Pipeline gate ─────────────────────────────────────────────────────────

    def should_invoke_ai(self, rule_engine_confidence: float | None) -> bool:
        """
        Return True when the rule-engine confidence is insufficient and AI
        should be consulted.

        A None confidence (rule engine produced no signal at all) is treated
        as below threshold — AI is always invoked in that case.
        """
        if rule_engine_confidence is None:
            return True
        return rule_engine_confidence < self._threshold

    # ── Analysis stage ────────────────────────────────────────────────────────

    def analyze(self, context: AISemanticContext) -> AISemanticResult | None:
        """
        Run the AI analysis stage for a single column.

        Returns None when:
        - No provider has been registered.
        - Provider raises NotImplementedError (no implementation wired yet).
        - Provider raises any other exception (logged; caller falls back to
          rule-engine result).

        Does NOT raise — callers can always fall back to the rule-engine result
        without defensive try/except at every call site.
        """
        if self._provider is None:
            logger.debug(
                "No AI provider registered; skipping AI analysis for %s.%s",
                context.table_fqn,
                context.column_name,
            )
            return None

        try:
            result = self._provider.analyze_metadata(context)
            logger.debug(
                "AI analysis complete for %s.%s — confidence=%.2f review_required=%s",
                context.table_fqn,
                context.column_name,
                result.confidence,
                result.review_required,
            )
            return result

        except NotImplementedError:
            logger.warning(
                "AI provider %r has not implemented analyze_metadata(); "
                "skipping AI analysis for %s.%s",
                type(self._provider).__name__,
                context.table_fqn,
                context.column_name,
            )
            return None

        except Exception:
            logger.exception(
                "AI provider raised an unexpected error for %s.%s; "
                "falling back to rule-engine result",
                context.table_fqn,
                context.column_name,
            )
            return None

    # ── Prompt access (for tests and future providers) ────────────────────────

    def build_prompt(self, context: AISemanticContext) -> tuple[str, str]:
        """
        Return (system_prompt, user_prompt) for the given context.

        Exposed so provider implementations and tests can inspect prompts
        without making live API calls.
        """
        return (
            self._prompt_builder.get_system_prompt(),
            self._prompt_builder.build_metadata_analysis_prompt(context),
        )

    # ── JSON validation (static — usable by providers and tests) ─────────────

    @staticmethod
    def validate_result_json(raw: dict[str, Any]) -> AISemanticResult:
        """
        Parse and validate a raw dict (typically decoded from an AI JSON response)
        into an AISemanticResult.

        Raises:
            ValueError: if required keys are absent, confidence is out of range,
                        or reasoning is empty.
        """
        required = {
            "business_name",
            "description",
            "domain",
            "entity",
            "confidence",
            "reasoning",
        }
        missing = required - raw.keys()
        if missing:
            raise ValueError(
                f"AI result is missing required keys: {sorted(missing)}"
            )

        confidence = float(raw["confidence"])
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(
                f"AI result confidence is out of range [0.0, 1.0]: {confidence}"
            )

        reasoning = raw["reasoning"]
        if not isinstance(reasoning, list) or len(reasoning) == 0:
            raise ValueError(
                "AI result reasoning must be a non-empty list of strings"
            )

        return AISemanticResult(
            business_name=str(raw["business_name"]),
            description=str(raw["description"]),
            domain=str(raw["domain"]),
            entity=str(raw["entity"]),
            confidence=confidence,
            reasoning=tuple(str(r) for r in reasoning),
            review_required=bool(raw.get("review_required", True)),
        )
