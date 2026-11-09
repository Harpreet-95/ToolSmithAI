"""
AI enrichment hook stub for the Dynamic Tool Creation Engine.

Future responsibility:
  Apply AI-powered enrichment at planning and post-run stages.

  before_plan  — optionally classify or rewrite the incoming intent for
                 better planner accuracy (e.g. expand abbreviations, detect
                 ambiguity, resolve entity references).
  after_plan   — if tool_def.ai.allow_enrichment is True, call the configured
                 model (tool_def.ai.enrichment_model) to:
                   • improve description / suggested_name
                   • infer missing required_inputs
                   • refine node params or template_refs
                 The enrichment model may only modify safe fields; it must not
                 change tool status, approval spec, or rbac spec.
  before_run   — no enrichment needed; reserved for future prompt injection
                 guards or content-safety checks.
  after_run    — optionally summarise run_record outputs in natural language
                 and attach the summary to run_record.outputs['ai_summary'].
  on_failure   — optionally generate a human-readable diagnosis of the failure.

When implemented, this hook will call the Anthropic SDK (or the configured
provider) using the model named in tool_def.ai.enrichment_model.
It must degrade gracefully (log + skip) when the AI service is unavailable
so that the engine never hard-fails due to enrichment being down.

Currently a no-op — no AI calls are made, no API keys are required.
"""

from __future__ import annotations

from core.engine.contracts import ExecutionContext, RunRecord, ToolDefinition
from core.engine.hooks.base import EngineHook


class AIHook(EngineHook):
    """
    AI enrichment hook.

    All methods are no-ops until the AI provider integration is wired in.
    Override individual lifecycle methods to add enrichment without touching
    any other part of the engine.

    Must not call any external service or require any API key in its current
    no-op form.
    """

    def before_plan(self, intent: str, context: dict) -> None:
        """Future: preprocess or classify intent before the planner runs."""

    def after_plan(self, tool_def: ToolDefinition, context: dict) -> None:
        """Future: enrich tool_def fields via AI if tool_def.ai.allow_enrichment."""

    def before_run(self, tool_def: ToolDefinition, context: ExecutionContext) -> None:
        """Future: content-safety or prompt-injection guard before execution."""

    def after_run(self, run_record: RunRecord, context: ExecutionContext) -> None:
        """Future: generate AI narrative summary of run outputs."""

    def on_failure(self, error: Exception, context: ExecutionContext) -> None:
        """Future: generate AI-assisted failure diagnosis."""
