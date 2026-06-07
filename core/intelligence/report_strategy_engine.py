"""
core/intelligence/report_strategy_engine.py

Report Strategy Engine — Phase 1 Foundation.

Defines the ReportStrategy and NarrativeConfig dataclasses plus the seven
canonical intent-type constants.  resolve_report_strategy() is the single
public entry point; in Phase 1 it always returns FULL_INTELLIGENCE so
behaviour is identical to the current pipeline.

Classification logic, section scoring, chart scoring, and narrative config
population are all deferred to later phases.  Nothing in this module
modifies any existing file or changes any live behaviour.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Canonical intent-type constants
# ---------------------------------------------------------------------------

EXECUTIVE_BRIEF    = "executive_brief"
KPI_SCORECARD      = "kpi_scorecard"
ANOMALY_FOCUS      = "anomaly_focus"
TREND_MONITORING   = "trend_monitoring"
DATA_QUALITY       = "data_quality"
VISUAL_DASHBOARD   = "visual_dashboard"
FULL_INTELLIGENCE  = "full_intelligence"

_ALL_INTENT_TYPES: frozenset[str] = frozenset({
    EXECUTIVE_BRIEF,
    KPI_SCORECARD,
    ANOMALY_FOCUS,
    TREND_MONITORING,
    DATA_QUALITY,
    VISUAL_DASHBOARD,
    FULL_INTELLIGENCE,
})


# ---------------------------------------------------------------------------
# NarrativeConfig
# ---------------------------------------------------------------------------

@dataclass
class NarrativeConfig:
    """Controls content density and emphasis within section builders.

    Phase 1: all fields carry defaults that reproduce current behaviour.
    Phase 4 will pass this object into section builders to differentiate
    content per intent.

    Fields
    ------
    items_per_section : int
        Maximum items (bullet points, anomalies, recommendations) surfaced
        per section.  Current builders cap at 5–8 internally; this provides
        a strategy-level override.
    show_evidence : bool
        When True, section builders include raw evidence strings (column names,
        counts, percentages).  False collapses to summary-level prose only.
    emphasize_risks : bool
        When True, high-severity anomalies and high-priority recommendations
        are surfaced before neutral findings within a section.
    suppress_types : list[str]
        Section types that this strategy considers irrelevant and should be
        excluded from the final report.  Empty list = no suppression.
    """
    items_per_section: int       = 6
    show_evidence:     bool      = True
    emphasize_risks:   bool      = True
    suppress_types:    list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ReportStrategy
# ---------------------------------------------------------------------------

@dataclass
class ReportStrategy:
    """Single authoritative object that shapes every aspect of report output.

    Resolved once per report execution by resolve_report_strategy() and
    threaded to ordering, filtering, chart selection, and (Phase 4) section
    builders.

    Fields
    ------
    intent_type : str
        One of the seven canonical constants defined in this module.
    report_style : str
        Maps to the existing report_planner style names for backward
        compatibility with the report_plan response key.
    audience : str
        "executive" | "analyst" | "operations" | "general"
    detail_level : str
        "low" | "medium" | "high"
    section_allowlist : list[str] | None
        Explicit list of section types to include.  None means all sections
        (no filtering).  Post-generation callers may further narrow this via
        the selected_sections parameter on generate_dataset_report().
    section_scores : dict[str, int]
        Priority weight per section type.  Higher = earlier in the report.
        Consumed by reorder_sections() in report_generator.py.
    viz_type_scores : dict[str, int]
        Preference weight per chart type.  Consumed by _build_chart_sections().
        0 = suppress that chart type entirely.
    max_sections : int
        Budget cap applied after reordering.  Highest-priority sections are
        kept; overflow is dropped.
    narrative_config : NarrativeConfig
        Controls content density and emphasis.  Passed into section builders
        from Phase 4 onwards.
    source : str
        Traceability label — how the strategy was resolved.
        "intent_keyword" | "dataset_signal" | "fallback"
    """
    intent_type:       str
    report_style:      str
    audience:          str
    detail_level:      str
    section_allowlist: list[str] | None
    section_scores:    dict[str, int]
    viz_type_scores:   dict[str, int]
    max_sections:      int
    narrative_config:  NarrativeConfig
    source:            str


# ---------------------------------------------------------------------------
# Phase 1 fallback strategy — full intelligence, no filtering, no reordering
# ---------------------------------------------------------------------------

def _full_intelligence_strategy() -> ReportStrategy:
    """Return a ReportStrategy that reproduces the current default behaviour.

    section_allowlist=None  → no section filtering (identical to today).
    section_scores={}       → reorder_sections() leaves order unchanged.
    viz_type_scores={}      → _build_chart_sections() uses its own defaults.
    max_sections=0          → no budget cap (0 is the sentinel for "no limit").
    """
    return ReportStrategy(
        intent_type       = FULL_INTELLIGENCE,
        report_style      = "analyst_deep_dive",
        audience          = "analyst",
        detail_level      = "high",
        section_allowlist = None,
        section_scores    = {},
        viz_type_scores   = {},
        max_sections      = 0,
        narrative_config  = NarrativeConfig(),
        source            = "fallback",
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def resolve_report_strategy(
    intent_text:         str | None,
    semantic_profile:    list[dict],
    date_profile:        dict,
    numeric_profile:     dict,
    categorical_profile: dict,
) -> ReportStrategy:
    """Resolve a ReportStrategy from intent text and dataset profiles.

    Phase 1: always returns the FULL_INTELLIGENCE fallback strategy.
    No classification, no keyword scoring, no chart logic.

    Subsequent phases will add:
      Phase 2 — intent keyword scoring → non-default intent types + section scores
      Phase 3 — section_allowlist population + suppression logic
      Phase 4 — narrative_config population per intent type

    Args:
        intent_text:          Raw user intent string, or None.
        semantic_profile:     List of semantic column descriptors.
        date_profile:         Date column analysis dict.
        numeric_profile:      Numeric column statistics dict.
        categorical_profile:  Categorical column frequency dicts.

    Returns:
        A valid ReportStrategy.  Never raises.
    """
    try:
        return _full_intelligence_strategy()
    except Exception:
        return _full_intelligence_strategy()
