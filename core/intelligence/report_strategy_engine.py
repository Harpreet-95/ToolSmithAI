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

from dataclasses import dataclass, field, replace as _dc_replace

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

    All fields carry defaults that reproduce current behaviour when unset.
    Section builders read these fields to differentiate content per intent.

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
    executive_language : bool
        When True, suppress raw column names and statistical jargon in favour
        of business-friendly labels.  Default False preserves analyst detail.
    recommendation_limit : int
        Hard cap on the number of recommendations surfaced.  Independent of
        items_per_section so recs can be tuned separately.  Default 5.
    verbosity : str
        "minimal" | "standard" | "detailed".  Controls how many items are
        produced in takeaway / risk / opportunity lists.  Default "standard".
    emphasize_opportunities : bool
        When True, opportunity items are surfaced before risk items.  Inverse
        of emphasize_risks; intended for positive-framing intents.
    suppress_low_confidence : bool
        When True, medium-confidence recommendations and low-strength trends
        are dropped.  Intended for executive audiences.
    """
    items_per_section:       int       = 6
    show_evidence:           bool      = True
    emphasize_risks:         bool      = True
    suppress_types:          list[str] = field(default_factory=list)
    executive_language:      bool      = False
    recommendation_limit:    int       = 5
    verbosity:               str       = "standard"
    emphasize_opportunities: bool      = False
    suppress_low_confidence: bool      = False


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
# Intent keyword signals
# ---------------------------------------------------------------------------
# Each tuple: (phrase_to_match, intent_type, weight).
# Phrases are matched as lowercased substrings of the full intent text.
# Scores accumulate across all matching phrases; highest total wins.
# Longer / more-specific phrases are listed first so they accumulate weight
# before shorter overlapping substrings also fire.

_INTENT_KEYWORD_SIGNALS: list[tuple[str, str, float]] = [
    # ── executive_brief ───────────────────────────────────────────────────────
    ("exec summary",      EXECUTIVE_BRIEF,   1.5),
    ("summary only",      EXECUTIVE_BRIEF,   1.3),
    ("leadership summary",EXECUTIVE_BRIEF,   1.4),
    ("management overview",EXECUTIVE_BRIEF,  1.3),
    ("quick summary",     EXECUTIVE_BRIEF,   1.2),
    ("quick overview",    EXECUTIVE_BRIEF,   1.1),
    ("at a glance",       EXECUTIVE_BRIEF,   1.2),
    ("high-level",        EXECUTIVE_BRIEF,   1.1),
    ("high level",        EXECUTIVE_BRIEF,   1.1),
    ("one-page",          EXECUTIVE_BRIEF,   1.5),
    ("one page",          EXECUTIVE_BRIEF,   1.5),
    ("top level",         EXECUTIVE_BRIEF,   1.0),
    ("executive",         EXECUTIVE_BRIEF,   1.2),
    ("c-suite",           EXECUTIVE_BRIEF,   1.2),
    ("board",             EXECUTIVE_BRIEF,   1.1),
    ("leadership",        EXECUTIVE_BRIEF,   1.0),
    ("brief",             EXECUTIVE_BRIEF,   1.0),
    ("ceo",               EXECUTIVE_BRIEF,   1.3),
    ("cfo",               EXECUTIVE_BRIEF,   1.1),
    # ── kpi_scorecard ─────────────────────────────────────────────────────────
    ("kpi focused",       KPI_SCORECARD,     1.8),
    ("kpi only",          KPI_SCORECARD,     1.8),
    ("kpi summary",       KPI_SCORECARD,     1.8),
    ("metrics only",      KPI_SCORECARD,     1.5),
    ("key metrics",       KPI_SCORECARD,     1.2),
    ("top metrics",       KPI_SCORECARD,     1.2),
    ("scorecard",         KPI_SCORECARD,     1.3),
    ("kpis",              KPI_SCORECARD,     1.5),
    ("kpi",               KPI_SCORECARD,     1.3),
    # ── anomaly_focus ─────────────────────────────────────────────────────────
    ("anomalies",         ANOMALY_FOCUS,     1.5),
    ("anomaly",           ANOMALY_FOCUS,     1.5),
    ("outliers",          ANOMALY_FOCUS,     1.2),
    ("outlier",           ANOMALY_FOCUS,     1.2),
    ("abnormal",          ANOMALY_FOCUS,     1.0),
    ("irregular",         ANOMALY_FOCUS,     1.0),
    ("unusual",           ANOMALY_FOCUS,     1.0),
    ("fraud",             ANOMALY_FOCUS,     1.3),
    ("spike",             ANOMALY_FOCUS,     1.0),
    ("detect",            ANOMALY_FOCUS,     0.9),
    # ── trend_monitoring ──────────────────────────────────────────────────────
    ("drift detection",   TREND_MONITORING,  1.8),
    ("week over week",    TREND_MONITORING,  1.4),
    ("month over month",  TREND_MONITORING,  1.4),
    ("trend analysis",    TREND_MONITORING,  1.4),
    ("analyze trends",    TREND_MONITORING,  1.3),
    ("time-series",       TREND_MONITORING,  1.3),
    ("time series",       TREND_MONITORING,  1.3),
    ("performance over",  TREND_MONITORING,  1.2),
    ("historical",        TREND_MONITORING,  1.2),
    ("over time",         TREND_MONITORING,  1.0),
    ("monitoring",        TREND_MONITORING,  1.5),
    ("tracking",          TREND_MONITORING,  1.0),
    ("monitor",           TREND_MONITORING,  1.3),
    ("drift",             TREND_MONITORING,  1.5),
    ("trend",             TREND_MONITORING,  1.1),
    # ── data_quality ──────────────────────────────────────────────────────────
    ("data quality",      DATA_QUALITY,      1.8),
    ("quality report",    DATA_QUALITY,      1.6),
    ("missing values",    DATA_QUALITY,      1.5),
    ("null values",       DATA_QUALITY,      1.3),
    ("completeness",      DATA_QUALITY,      1.5),
    ("data hygiene",      DATA_QUALITY,      1.6),
    ("hygiene",           DATA_QUALITY,      1.4),
    ("cleanse",           DATA_QUALITY,      1.4),
    ("cleaning",          DATA_QUALITY,      1.3),
    ("validation",        DATA_QUALITY,      1.3),
    ("validate",          DATA_QUALITY,      1.2),
    ("integrity",         DATA_QUALITY,      1.2),
    # ── visual_dashboard ──────────────────────────────────────────────────────
    ("charts only",       VISUAL_DASHBOARD,  1.8),
    ("visual report",     VISUAL_DASHBOARD,  1.3),
    ("visualization",     VISUAL_DASHBOARD,  1.2),
    ("dashboard",         VISUAL_DASHBOARD,  1.5),
    ("visualize",         VISUAL_DASHBOARD,  1.0),
    ("visual",            VISUAL_DASHBOARD,  1.2),
    ("graphs",            VISUAL_DASHBOARD,  0.9),
    ("charts",            VISUAL_DASHBOARD,  0.9),
    ("graph",             VISUAL_DASHBOARD,  0.9),
    ("chart",             VISUAL_DASHBOARD,  1.1),
    ("plot",              VISUAL_DASHBOARD,  1.0),
    # ── full_intelligence — explicit signals (scored but treated as fallback) ──
    ("all sections",      FULL_INTELLIGENCE, 1.5),
    ("complete report",   FULL_INTELLIGENCE, 1.2),
    ("full analysis",     FULL_INTELLIGENCE, 1.4),
    ("full report",       FULL_INTELLIGENCE, 1.2),
    ("deep-dive",         FULL_INTELLIGENCE, 1.5),
    ("deep dive",         FULL_INTELLIGENCE, 1.5),
    ("in-depth",          FULL_INTELLIGENCE, 1.3),
    ("in depth",          FULL_INTELLIGENCE, 1.3),
    ("comprehensive",     FULL_INTELLIGENCE, 1.1),
    ("everything",        FULL_INTELLIGENCE, 1.0),
    ("detailed",          FULL_INTELLIGENCE, 1.2),
]

# ---------------------------------------------------------------------------
# Section priority scores per intent type
# ---------------------------------------------------------------------------
# Keys must match the `type` field stamped on sections in report_generator.py.
# Missing keys score 0 in reorder_sections().  FULL_INTELLIGENCE omitted so it
# falls back to the report_planner's own scores (zero-behaviour-change).
# Note: "missing_data" and "trend_insights" are forward-looking keys — they
# have no matching section type today (both render as "text") but will resolve
# once those sections get dedicated type stamps in a later phase.

_SECTION_SCORES_BY_INTENT: dict[str, dict[str, int]] = {
    EXECUTIVE_BRIEF: {
        "ai_dashboard":         100,
        "executive_summary":     95,
        "business_kpis":         90,
        "kpi":                   85,
        "recommendation":        80,
        "insight_priority":      75,
        "chart":                 70,
        "anomaly":               40,
        "trend":                 30,
        "predictive_readiness":  20,
        "historical_comparison": 15,
        "drift_detection":       10,
        "segmentation":           5,
        "drilldown_table":        5,
        "forecast":               0,
        "text":                   0,
    },
    KPI_SCORECARD: {
        "ai_dashboard":         100,
        "business_kpis":         95,
        "kpi":                   90,
        "executive_summary":     85,
        "chart":                 80,
        "recommendation":        70,
        "insight_priority":      60,
        "anomaly":               40,
        "trend":                 30,
        "segmentation":          25,
        "predictive_readiness":  15,
        "historical_comparison": 10,
        "text":                  10,
        "drift_detection":        5,
        "drilldown_table":        5,
        "forecast":               5,
    },
    ANOMALY_FOCUS: {
        "anomaly":              100,
        "drift_detection":       90,
        "insight_priority":      85,
        "recommendation":        80,
        "ai_dashboard":          75,
        "executive_summary":     60,
        "historical_comparison": 55,
        "kpi":                   40,
        "business_kpis":         35,
        "trend":                 30,
        "predictive_readiness":  20,
        "chart":                 15,
        "segmentation":          10,
        "drilldown_table":       10,
        "text":                  10,
        "forecast":               5,
    },
    TREND_MONITORING: {
        "trend":                100,
        "trend_insights":        95,   # forward-looking key
        "drift_detection":       90,
        "historical_comparison": 85,
        "forecast":              80,
        "chart":                 75,
        "ai_dashboard":          70,
        "insight_priority":      65,
        "anomaly":               55,
        "executive_summary":     45,
        "kpi":                   35,
        "business_kpis":         30,
        "recommendation":        25,
        "predictive_readiness":  20,
        "segmentation":          15,
        "text":                   5,
        "drilldown_table":        5,
    },
    DATA_QUALITY: {
        "missing_data":         100,   # forward-looking key
        "anomaly":               95,
        "drift_detection":       85,
        "insight_priority":      80,
        "recommendation":        70,
        "ai_dashboard":          65,
        "kpi":                   55,
        "executive_summary":     50,
        "historical_comparison": 45,
        "predictive_readiness":  40,
        "trend":                 25,
        "chart":                 20,
        "business_kpis":         15,
        "text":                  10,
        "segmentation":           5,
        "drilldown_table":        5,
        "forecast":               5,
    },
    VISUAL_DASHBOARD: {
        "chart":                100,
        "ai_dashboard":          95,
        "business_kpis":         90,
        "kpi":                   85,
        "executive_summary":     80,
        "insight_priority":      70,
        "trend":                 65,
        "segmentation":          60,
        "anomaly":               40,
        "recommendation":        35,
        "drilldown_table":       30,
        "historical_comparison": 20,
        "drift_detection":       15,
        "predictive_readiness":  10,
        "text":                   5,
        "forecast":               5,
    },
}

# ---------------------------------------------------------------------------
# Viz type preference scores per intent type
# ---------------------------------------------------------------------------
# Keys must match the chart_type strings used in _build_chart_sections()
# and read by _viz_score_for() in report_generator.py.
# 0 = suppress that chart type entirely.  Default when key is absent = 5.

_VIZ_TYPE_SCORES_BY_INTENT: dict[str, dict[str, int]] = {
    EXECUTIVE_BRIEF: {
        "bar":                7,
        "line":               6,
        "donut":              9,
        "pie":                5,
        "correlation_matrix": 0,
    },
    KPI_SCORECARD: {
        "bar":                8,
        "line":               5,
        "donut":              9,
        "pie":                5,
        "correlation_matrix": 0,
    },
    ANOMALY_FOCUS: {
        "bar":                8,
        "line":               4,
        "donut":              3,
        "pie":                3,
        "correlation_matrix": 9,
    },
    TREND_MONITORING: {
        "bar":                5,
        "line":               10,
        "donut":              3,
        "pie":                3,
        "correlation_matrix": 4,
    },
    DATA_QUALITY: {
        "bar":                9,
        "line":               2,
        "donut":              2,
        "pie":                2,
        "correlation_matrix": 8,
    },
    VISUAL_DASHBOARD: {
        "bar":                8,
        "line":               8,
        "donut":              9,
        "pie":                6,
        "correlation_matrix": 7,
    },
}

# ---------------------------------------------------------------------------
# Narrative config per intent type
# ---------------------------------------------------------------------------
# FULL_INTELLIGENCE omitted — falls back to NarrativeConfig() defaults so
# existing behaviour is unchanged for generic / unclassified reports.

_NARRATIVE_CONFIGS_BY_INTENT: dict[str, NarrativeConfig] = {
    EXECUTIVE_BRIEF: NarrativeConfig(
        items_per_section       = 3,
        show_evidence           = False,
        emphasize_risks         = False,
        emphasize_opportunities = True,
        executive_language      = True,
        recommendation_limit    = 3,
        verbosity               = "minimal",
        suppress_low_confidence = True,
    ),
    KPI_SCORECARD: NarrativeConfig(
        items_per_section       = 4,
        show_evidence           = False,
        emphasize_risks         = False,
        emphasize_opportunities = True,
        executive_language      = True,
        recommendation_limit    = 3,
        verbosity               = "minimal",
        suppress_low_confidence = True,
    ),
    ANOMALY_FOCUS: NarrativeConfig(
        items_per_section       = 8,
        show_evidence           = True,
        emphasize_risks         = True,
        emphasize_opportunities = False,
        executive_language      = False,
        recommendation_limit    = 5,
        verbosity               = "detailed",
        suppress_low_confidence = False,
    ),
    TREND_MONITORING: NarrativeConfig(
        items_per_section       = 6,
        show_evidence           = True,
        emphasize_risks         = False,
        emphasize_opportunities = False,
        executive_language      = False,
        recommendation_limit    = 4,
        verbosity               = "standard",
        suppress_low_confidence = False,
    ),
    DATA_QUALITY: NarrativeConfig(
        items_per_section       = 8,
        show_evidence           = True,
        emphasize_risks         = True,
        emphasize_opportunities = False,
        executive_language      = False,
        recommendation_limit    = 5,
        verbosity               = "detailed",
        suppress_low_confidence = False,
    ),
    VISUAL_DASHBOARD: NarrativeConfig(
        items_per_section       = 3,
        show_evidence           = False,
        emphasize_risks         = False,
        emphasize_opportunities = True,
        executive_language      = True,
        recommendation_limit    = 2,
        verbosity               = "minimal",
        suppress_low_confidence = True,
    ),
}

# Minimum cumulative score required to accept a non-fallback classification.
# A single weak signal (e.g. "chart" in "bar chart breakdown") scores 1.1 —
# just above threshold — so genuinely chart-focused requests are classified
# correctly without false positives from incidental mentions.
_MIN_SCORE: float = 1.0


def _classify_intent_text(intent_text: str | None) -> tuple[str, str]:
    """Score intent text against keyword signals; return (intent_type, source).

    Accumulates weights for all matching phrases and returns the intent type
    with the highest total score, provided it meets _MIN_SCORE.

    Returns (FULL_INTELLIGENCE, "fallback") when:
    - intent_text is absent or blank.
    - no phrase matches.
    - the top score is below _MIN_SCORE.
    - the winning type is FULL_INTELLIGENCE itself (explicit or implicit).
    """
    if not intent_text or not intent_text.strip():
        return FULL_INTELLIGENCE, "fallback"

    lowered = intent_text.lower()
    scores: dict[str, float] = {}

    for phrase, intent_type, weight in _INTENT_KEYWORD_SIGNALS:
        if phrase in lowered:
            scores[intent_type] = scores.get(intent_type, 0.0) + weight

    if not scores:
        return FULL_INTELLIGENCE, "fallback"

    best = max(scores, key=lambda k: scores[k])

    if scores[best] < _MIN_SCORE or best == FULL_INTELLIGENCE:
        return FULL_INTELLIGENCE, "fallback"

    return best, "intent_keyword"


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
        intent_type, source = _classify_intent_text(intent_text)
        base = _full_intelligence_strategy()
        if intent_type == FULL_INTELLIGENCE:
            return base
        section_scores   = _SECTION_SCORES_BY_INTENT.get(intent_type, {})
        viz_type_scores  = _VIZ_TYPE_SCORES_BY_INTENT.get(intent_type, {})
        narrative_config = _NARRATIVE_CONFIGS_BY_INTENT.get(intent_type, NarrativeConfig())
        report_style     = "anomaly_report" if intent_type == DATA_QUALITY else base.report_style
        return _dc_replace(
            base,
            intent_type      = intent_type,
            source           = source,
            section_scores   = section_scores,
            viz_type_scores  = viz_type_scores,
            narrative_config = narrative_config,
            report_style     = report_style,
        )
    except Exception:
        return _full_intelligence_strategy()
