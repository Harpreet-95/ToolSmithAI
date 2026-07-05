from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RelationshipSignal:
    """A single foreign-key relationship associated with the column's table."""

    table_fqn: str
    relationship_type: str  # "references" | "referenced_by"


@dataclass(frozen=True)
class ReviewTask:
    """A historical governance review action on this column/table."""

    task_id: str
    action: str  # "approved" | "rejected" | "pending"
    reviewed_at: str | None


@dataclass
class AISemanticContext:
    """
    Normalized, provider-agnostic context object passed to the AI analysis stage.

    Populated by SemanticIntelligenceService from existing profiling, domain,
    entity, and dictionary data.  The AI provider must not query the database —
    all signals are pre-loaded here.
    """

    # --- Identity ----------------------------------------------------------
    source_id: int
    schema_name: str
    table_name: str
    table_fqn: str
    column_name: str

    # --- Business metadata -------------------------------------------------
    business_name: str | None
    existing_description: str | None
    existing_domain: str | None
    existing_entity: str | None

    # --- Rule-engine outputs (what triggered the AI call) ------------------
    semantic_type: str | None            # SemanticType enum value, e.g. "EMAIL"
    semantic_confidence: float | None
    rule_engine_domain: str | None
    rule_engine_entity: str | None
    rule_engine_confidence: float | None
    rule_engine_evidence: list[str] = field(default_factory=list)

    # --- Data quality signals ----------------------------------------------
    quality_score: float | None = None   # 0–100
    quality_grade: str | None = None     # A/B/C/D/F
    completeness_score: float | None = None

    # --- Cardinality & distribution ----------------------------------------
    cardinality_tier: str | None = None  # CardinalityTier enum value
    distinct_count: int | None = None
    distinct_percentage: float | None = None
    uniqueness_score: float | None = None
    null_percentage: float | None = None
    empty_string_count: int | None = None
    distribution_shape: str | None = None  # symmetric|left_skewed|right_skewed|constant|sparse|highly_skewed

    # --- PII signals -------------------------------------------------------
    pii_confirmed: bool = False
    pii_signals: list[str] = field(default_factory=list)

    # --- Pattern detection -------------------------------------------------
    dominant_pattern: str | None = None
    pattern_coverage: float | None = None
    email_match_rate: float | None = None
    phone_match_rate: float | None = None

    # --- Value samples -----------------------------------------------------
    top_values: list[dict[str, Any]] = field(default_factory=list)   # [{value, count, percentage}]
    sample_values: list[str] = field(default_factory=list)

    # --- Structural relationships ------------------------------------------
    relationships: list[RelationshipSignal] = field(default_factory=list)

    # --- Review task history -----------------------------------------------
    review_history: list[ReviewTask] = field(default_factory=list)

    # --- Existing dictionary entry (if any) --------------------------------
    existing_dictionary: dict[str, Any] | None = None


@dataclass(frozen=True)
class AISemanticResult:
    """
    Structured output returned by an AISemanticProvider.

    The confidence field reflects the AI's own certainty, not the rule engine's.
    review_required must be True whenever confidence < 1.0 — callers may enforce
    a stricter policy but must never weaken it.
    """

    business_name: str
    description: str
    domain: str
    entity: str
    confidence: float          # 0.0–1.0
    reasoning: tuple[str, ...] # ordered highest-signal first, non-empty
    review_required: bool

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"AISemanticResult.confidence must be 0.0–1.0, got {self.confidence}"
            )
        if not self.reasoning:
            raise ValueError("AISemanticResult.reasoning must be a non-empty tuple")
