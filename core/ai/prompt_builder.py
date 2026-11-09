from __future__ import annotations

import json

from .models import AISemanticContext

# ---------------------------------------------------------------------------
# System prompt — provider-agnostic, used for all AI metadata analysis calls.
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are an expert data governance analyst specialising in enterprise metadata "
    "classification. You receive structured signals about a single database column "
    "and return a JSON classification. "
    "Rules: "
    "(1) Return ONLY valid JSON — no markdown, no prose, no code fences. "
    "(2) Every field in the required schema must be present. "
    "(3) confidence must be a float between 0.0 and 1.0. "
    "(4) reasoning must be a non-empty JSON array of concise strings, "
    "ordered from the strongest signal to the weakest. "
    "(5) Set review_required to true whenever confidence < 1.0. "
    "(6) Your description MUST be meaningfully different from and better than "
    "the Current Description. Do not copy, restate, or lightly paraphrase it. "
    "Write a specific, human-readable definition explaining what business concept "
    "this column captures and how it is used. "
    "If the available signals are insufficient to produce a genuinely better "
    "description, set confidence to 0.0 and explain in reasoning what additional "
    "context (e.g. sample values, table purpose) would be needed."
)

_OUTPUT_SCHEMA = """\
{
  "business_name": "<human-readable column name>",
  "description": "<1-2 sentence business definition>",
  "domain": "<business domain, e.g. Student Lifecycle>",
  "entity": "<primary entity, e.g. Student>",
  "confidence": 0.91,
  "reasoning": [
    "<strongest signal>",
    "<second signal>",
    "<third signal>"
  ],
  "review_required": true
}"""


class PromptBuilder:
    """
    Centralises all prompt construction for AI metadata analysis.

    No prompt text lives anywhere else in the codebase.  Business logic
    constructs an AISemanticContext; this class turns it into the
    (system_prompt, user_prompt) pair that goes to the AI provider.
    """

    def get_system_prompt(self) -> str:
        return _SYSTEM_PROMPT

    def build_metadata_analysis_prompt(self, context: AISemanticContext) -> str:
        """
        Build the user-turn prompt for a single column classification request.

        Sections are emitted only when the relevant data is present, keeping
        token usage proportional to signal richness.
        """
        parts: list[str] = []

        # ── Identity ────────────────────────────────────────────────────────
        parts.append("## Column to Classify")
        parts.append(f"Source ID  : {context.source_id}")
        parts.append(f"Schema     : {context.schema_name}")
        parts.append(f"Table      : {context.table_name}")
        parts.append(f"Column     : {context.column_name}")
        if context.business_name:
            parts.append(f"Business Name: {context.business_name}")
        if context.existing_description:
            parts.append(f"Current Description: {context.existing_description}")

        # ── Rule-engine output (reason AI was invoked) ───────────────────────
        parts.append("\n## Rule Engine Output  [low confidence — AI review requested]")
        parts.append(f"Semantic Type      : {context.semantic_type or 'Unknown'}")
        parts.append(f"Semantic Confidence: {context.semantic_confidence}")
        parts.append(f"Domain             : {context.rule_engine_domain or 'Unknown'}")
        parts.append(f"Entity             : {context.rule_engine_entity or 'Unknown'}")
        parts.append(f"Rule Confidence    : {context.rule_engine_confidence}")
        if context.rule_engine_evidence:
            parts.append(f"Evidence           : {'; '.join(context.rule_engine_evidence)}")
        if context.existing_domain and context.existing_domain != context.rule_engine_domain:
            parts.append(f"Prior Domain (DB)  : {context.existing_domain}")
        if context.existing_entity and context.existing_entity != context.rule_engine_entity:
            parts.append(f"Prior Entity (DB)  : {context.existing_entity}")

        # ── Data quality ─────────────────────────────────────────────────────
        parts.append("\n## Data Quality")
        parts.append(f"Quality Score      : {context.quality_score}")
        parts.append(f"Quality Grade      : {context.quality_grade}")
        parts.append(f"Completeness Score : {context.completeness_score}")

        # ── Statistical profile ───────────────────────────────────────────────
        parts.append("\n## Statistical Profile")
        parts.append(f"Null %             : {context.null_percentage}")
        parts.append(f"Cardinality Tier   : {context.cardinality_tier}")
        parts.append(f"Distinct Count     : {context.distinct_count}")
        parts.append(f"Distinct %         : {context.distinct_percentage}")
        parts.append(f"Uniqueness Score   : {context.uniqueness_score}")
        parts.append(f"Distribution Shape : {context.distribution_shape}")
        if context.empty_string_count is not None:
            parts.append(f"Empty String Count : {context.empty_string_count}")

        # ── PII signals (only if present) ─────────────────────────────────────
        if context.pii_confirmed or context.pii_signals:
            parts.append("\n## PII Signals")
            parts.append(f"PII Confirmed: {context.pii_confirmed}")
            if context.pii_signals:
                parts.append(f"Signals      : {', '.join(context.pii_signals)}")

        # ── Pattern detection (only if dominant pattern found) ────────────────
        if context.dominant_pattern:
            parts.append("\n## Pattern Detection")
            parts.append(f"Dominant Pattern : {context.dominant_pattern}")
            parts.append(f"Pattern Coverage : {context.pattern_coverage}")
            if context.email_match_rate is not None:
                parts.append(f"Email Match Rate : {context.email_match_rate}")
            if context.phone_match_rate is not None:
                parts.append(f"Phone Match Rate : {context.phone_match_rate}")

        # ── Top values (capped at 10 rows) ────────────────────────────────────
        if context.top_values:
            parts.append("\n## Top Values")
            for tv in context.top_values[:10]:
                pct = tv.get("percentage", 0)
                parts.append(f"  {tv.get('value')!r:40s} {pct:.1f}%")

        # ── Sample values ─────────────────────────────────────────────────────
        if context.sample_values:
            formatted = ", ".join(repr(v) for v in context.sample_values[:5])
            parts.append(f"\nSample Values: {formatted}")

        # ── Structural relationships ───────────────────────────────────────────
        if context.relationships:
            parts.append("\n## Table Relationships")
            for rel in context.relationships:
                parts.append(f"  {rel.relationship_type:15s}: {rel.table_fqn}")

        # ── Review task history ───────────────────────────────────────────────
        if context.review_history:
            parts.append("\n## Governance Review History")
            for rh in context.review_history:
                parts.append(f"  [{rh.action:8s}] {rh.task_id}  at {rh.reviewed_at}")

        # ── Existing dictionary entry ─────────────────────────────────────────
        if context.existing_dictionary:
            parts.append("\n## Existing Dictionary Entry")
            parts.append(json.dumps(context.existing_dictionary, indent=2))

        # ── Required output schema ────────────────────────────────────────────
        parts.append("\n## Required JSON Output")
        parts.append(_OUTPUT_SCHEMA)

        return "\n".join(parts)
