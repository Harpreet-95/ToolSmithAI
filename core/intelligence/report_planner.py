"""
core/intelligence/report_planner.py

Adaptive Report Planner + Intelligent Visualization Planner for ToolSmithAI.

Deterministically infers:
  - report_style (one of 8 supported styles)
  - audience (executive | analyst | operations | general)
  - section priority order
  - visualization type preferences
  - detail level and narrative depth

Inputs:
  - intent_text (user's natural language request)
  - semantic_profile (from semantic_classifier)
  - date_profile, numeric_profile, categorical_profile (from upload profiling)

No ML. No AI calls. No external dependencies.
All logic is keyword-heuristic + rule-based + deterministic.

Supported report styles:
  executive_brief     — short, KPI-first, minimal charts, executive audience
  visual_dashboard    — charts + KPI cards, minimal prose
  analyst_deep_dive   — full detail, all sections, tables + charts
  table_heavy_report  — drilldown/segmentation tables prominent, fewer charts
  operational_report  — anomaly + recommendation + trend hero sections
  anomaly_report      — anomaly section first, risk-focused
  kpi_summary         — business KPIs + structural KPIs, minimal else
  monitoring_report   — drift + historical comparison + trend hero sections

Future hooks:
  - plan_report() output feeds adaptive UI rendering
  - viz_type_scores feeds intelligent chart selector
  - layout_metadata feeds collapsible section decisions
  - audience field feeds narrative tone in AI summary layer
  - detail_level feeds section truncation in realtime dashboards
"""

from typing import Any

# ── Report Style Registry ──────────────────────────────────────────────────────

_REPORT_STYLES = frozenset({
    "executive_brief",
    "visual_dashboard",
    "analyst_deep_dive",
    "table_heavy_report",
    "operational_report",
    "anomaly_report",
    "kpi_summary",
    "monitoring_report",
})

# Fallback style — full detail, all sections
_DEFAULT_STYLE = "analyst_deep_dive"

# ── Section Priority Scores by Style ─────────────────────────────────────────
# Higher score → earlier in report. Unlisted types get score 0 (appear last).
# The generator builds ALL sections regardless; this only controls ordering.

_SECTION_SCORES: dict[str, dict[str, int]] = {
    "executive_brief": {
        "ai_dashboard":           10,
        "executive_summary":       9,
        "kpi":                     8,
        "business_kpis":           8,
        "recommendation":          7,
        "insight_priority":        6,
        "anomaly":                 5,
        "trend":                   4,
        "chart":                   3,
        "predictive_readiness":    2,
        "historical_comparison":   2,
        "drift_detection":         1,
        "forecast":                1,
        "text":                    0,
    },
    "visual_dashboard": {
        "ai_dashboard":           10,
        "kpi":                     9,
        "business_kpis":           9,
        "chart":                   8,
        "segmentation":            8,
        "executive_summary":       7,
        "trend":                   7,
        "anomaly":                 6,
        "recommendation":          5,
        "insight_priority":        5,
        "predictive_readiness":    4,
        "historical_comparison":   3,
        "drift_detection":         3,
        "forecast":                4,
        "drilldown_table":         2,
        "text":                    1,
    },
    "analyst_deep_dive": {
        "executive_summary":      10,
        "ai_dashboard":            9,
        "insight_priority":        9,
        "business_kpis":           8,
        "kpi":                     8,
        "anomaly":                 8,
        "trend":                   7,
        "recommendation":          7,
        "segmentation":            7,
        "drilldown_table":         7,
        "chart":                   6,
        "predictive_readiness":    6,
        "historical_comparison":   6,
        "drift_detection":         6,
        "forecast":                5,
        "text":                    4,
    },
    "table_heavy_report": {
        "executive_summary":      10,
        "kpi":                     9,
        "business_kpis":           8,
        "drilldown_table":         8,
        "segmentation":            8,
        "anomaly":                 7,
        "recommendation":          7,
        "historical_comparison":   6,
        "text":                    5,
        "drift_detection":         5,
        "trend":                   4,
        "predictive_readiness":    3,
        "chart":                   2,
        "ai_dashboard":            1,
        "forecast":                1,
    },
    "operational_report": {
        "anomaly":                10,
        "recommendation":          9,
        "kpi":                     8,
        "trend":                   8,
        "business_kpis":           7,
        "ai_dashboard":            7,
        "insight_priority":        7,
        "drift_detection":         6,
        "historical_comparison":   6,
        "executive_summary":       5,
        "chart":                   4,
        "segmentation":            4,
        "text":                    3,
        "predictive_readiness":    3,
        "forecast":                3,
    },
    "anomaly_report": {
        "anomaly":                10,
        "insight_priority":        9,
        "ai_dashboard":            8,
        "recommendation":          8,
        "drift_detection":         7,
        "trend":                   6,
        "executive_summary":       5,
        "kpi":                     5,
        "business_kpis":           4,
        "historical_comparison":   4,
        "chart":                   3,
        "text":                    3,
        "predictive_readiness":    2,
        "forecast":                1,
    },
    "kpi_summary": {
        "kpi":                    10,
        "business_kpis":          10,
        "ai_dashboard":            8,
        "executive_summary":       8,
        "insight_priority":        7,
        "recommendation":          6,
        "chart":                   5,
        "anomaly":                 4,
        "trend":                   3,
        "segmentation":            3,
        "text":                    2,
        "predictive_readiness":    2,
        "historical_comparison":   2,
        "drilldown_table":         1,
        "forecast":                1,
    },
    "monitoring_report": {
        "drift_detection":        10,
        "historical_comparison":   9,
        "anomaly":                 9,
        "trend":                   8,
        "ai_dashboard":            8,
        "kpi":                     7,
        "insight_priority":        7,
        "business_kpis":           6,
        "recommendation":          6,
        "executive_summary":       5,
        "forecast":                5,
        "chart":                   4,
        "text":                    3,
        "predictive_readiness":    3,
        "segmentation":            2,
    },
}

# ── Visualization Type Scores by Style ────────────────────────────────────────
# Metadata for adaptive rendering and chart selector.
# Higher = prefer this visualization type in this style.
# Avoid: pie on high-cardinality, duplicate bar charts, visuals without value.

_VIZ_SCORES: dict[str, dict[str, int]] = {
    "executive_brief": {
        "kpi_card":           10,
        "line":                6,
        "bar":                 5,
        "donut":               4,
        "pie":                 3,
        "table":               2,
        "drilldown_table":     1,
        "stacked_bar":         1,
        "grouped_line":        1,
        "correlation_matrix":  0,
    },
    "visual_dashboard": {
        "kpi_card":           10,
        "line":                9,
        "bar":                 8,
        "stacked_bar":         7,
        "grouped_line":        7,
        "donut":               6,
        "pie":                 5,
        "correlation_matrix":  5,
        "table":               3,
        "drilldown_table":     3,
    },
    "analyst_deep_dive": {
        "kpi_card":            7,
        "line":                8,
        "bar":                 8,
        "stacked_bar":         8,
        "grouped_line":        8,
        "table":               8,
        "drilldown_table":     9,
        "correlation_matrix":  9,
        "pie":                 4,
        "donut":               4,
    },
    "table_heavy_report": {
        "table":              10,
        "drilldown_table":    10,
        "kpi_card":            6,
        "bar":                 5,
        "line":                4,
        "stacked_bar":         3,
        "grouped_line":        3,
        "pie":                 2,
        "donut":               2,
        "correlation_matrix":  3,
    },
    "operational_report": {
        "kpi_card":            9,
        "bar":                 8,
        "line":                7,
        "table":               7,
        "stacked_bar":         6,
        "drilldown_table":     6,
        "grouped_line":        5,
        "correlation_matrix":  2,
        "pie":                 2,
        "donut":               2,
    },
    "anomaly_report": {
        "kpi_card":            7,
        "bar":                 8,
        "table":               9,
        "line":                6,
        "drilldown_table":     7,
        "stacked_bar":         4,
        "grouped_line":        3,
        "pie":                 2,
        "donut":               2,
        "correlation_matrix":  4,
    },
    "kpi_summary": {
        "kpi_card":           10,
        "bar":                 7,
        "donut":               6,
        "pie":                 5,
        "line":                6,
        "table":               4,
        "drilldown_table":     3,
        "stacked_bar":         4,
        "grouped_line":        4,
        "correlation_matrix":  2,
    },
    "monitoring_report": {
        "line":               10,
        "kpi_card":            8,
        "bar":                 7,
        "stacked_bar":         7,
        "grouped_line":        7,
        "table":               7,
        "drilldown_table":     6,
        "correlation_matrix":  5,
        "pie":                 1,
        "donut":               2,
    },
}

# ── Visualization Selection Rules ─────────────────────────────────────────────
# Deterministic rules for when each chart type is appropriate.
# Used by future chart selector; documented here for audit/traceability.

VIZ_SELECTION_RULES: dict[str, dict[str, Any]] = {
    "kpi_card": {
        "use_when": ["numeric metric present", "financial column detected", "score/percentage column"],
        "avoid_when": ["no numeric columns"],
        "max_per_report": 8,
    },
    "line": {
        "use_when": ["time-series data available", "≥2 monthly data points", "trend over time needed"],
        "avoid_when": ["no date column", "fewer than 3 data points"],
        "max_per_report": 3,
    },
    "bar": {
        "use_when": ["categorical column with ≤20 distinct values", "comparing groups"],
        "avoid_when": ["cardinality > 20 (use table instead)", "single value column"],
        "max_per_report": 3,
    },
    "stacked_bar": {
        "use_when": ["2+ categorical dimensions", "part-of-whole breakdown needed"],
        "avoid_when": ["single categorical column", "numeric-only dataset"],
        "max_per_report": 2,
    },
    "pie": {
        "use_when": ["≤7 distinct values in categorical column", "part-of-whole story"],
        "avoid_when": ["cardinality > 7 (high-cardinality visual clutter)", "time-series data"],
        "max_per_report": 1,
    },
    "donut": {
        "use_when": ["≤7 categories", "KPI summary style", "compact display needed"],
        "avoid_when": ["cardinality > 7", "analyst_deep_dive style"],
        "max_per_report": 1,
    },
    "correlation_matrix": {
        "use_when": ["≥2 numeric columns", "analyst or deep-dive context"],
        "avoid_when": ["executive_brief style", "kpi_summary style", "single numeric column"],
        "max_per_report": 1,
    },
    "drilldown_table": {
        "use_when": ["segmentation data available", "dimension + metric combination"],
        "avoid_when": ["executive_brief style", "kpi_summary style"],
        "max_per_report": 3,
    },
    "forecast": {
        "use_when": ["≥6 monthly data points", "time-series confirmed"],
        "avoid_when": ["no date column", "fewer than 6 data points"],
        "max_per_report": 1,
    },
}

# ── Style Metadata ─────────────────────────────────────────────────────────────

_STYLE_META: dict[str, dict[str, Any]] = {
    "executive_brief": {
        "audience":            "executive",
        "detail_level":        "low",
        "visual_preference":   "kpi_only",
        "narrative_depth":     "minimal",
        "max_sections":        7,
        "hero_sections":       ["executive_summary", "kpi", "business_kpis"],
        "expandable_sections": ["predictive_readiness", "drift_detection", "chart"],
        "description":         "Concise executive view: KPI cards, summary, top recommendations only.",
    },
    "visual_dashboard": {
        "audience":            "general",
        "detail_level":        "medium",
        "visual_preference":   "visual_heavy",
        "narrative_depth":     "minimal",
        "max_sections":        12,
        "hero_sections":       ["kpi", "business_kpis", "chart"],
        "expandable_sections": ["drilldown_table", "text"],
        "description":         "Chart-first dashboard with KPI cards and minimal prose.",
    },
    "analyst_deep_dive": {
        "audience":            "analyst",
        "detail_level":        "high",
        "visual_preference":   "balanced",
        "narrative_depth":     "detailed",
        "max_sections":        20,
        "hero_sections":       ["executive_summary", "insight_priority", "anomaly"],
        "expandable_sections": [],
        "description":         "Full-detail report: all sections, tables, charts, deep analysis.",
    },
    "table_heavy_report": {
        "audience":            "analyst",
        "detail_level":        "high",
        "visual_preference":   "table_heavy",
        "narrative_depth":     "standard",
        "max_sections":        15,
        "hero_sections":       ["executive_summary", "drilldown_table", "segmentation"],
        "expandable_sections": ["chart", "predictive_readiness"],
        "description":         "Segmentation and drilldown tables first; charts secondary.",
    },
    "operational_report": {
        "audience":            "operations",
        "detail_level":        "medium",
        "visual_preference":   "balanced",
        "narrative_depth":     "standard",
        "max_sections":        12,
        "hero_sections":       ["anomaly", "recommendation", "trend"],
        "expandable_sections": ["predictive_readiness", "forecast"],
        "description":         "Operational focus: anomalies, recommendations, and trend signals first.",
    },
    "anomaly_report": {
        "audience":            "operations",
        "detail_level":        "medium",
        "visual_preference":   "balanced",
        "narrative_depth":     "standard",
        "max_sections":        10,
        "hero_sections":       ["anomaly", "insight_priority", "recommendation"],
        "expandable_sections": ["chart", "forecast"],
        "description":         "Risk-focused: data quality issues and anomalies as hero sections.",
    },
    "kpi_summary": {
        "audience":            "executive",
        "detail_level":        "low",
        "visual_preference":   "kpi_only",
        "narrative_depth":     "minimal",
        "max_sections":        8,
        "hero_sections":       ["kpi", "business_kpis", "executive_summary"],
        "expandable_sections": ["drilldown_table", "trend"],
        "description":         "KPI-focused summary: business metrics cards and executive narrative.",
    },
    "monitoring_report": {
        "audience":            "operations",
        "detail_level":        "medium",
        "visual_preference":   "visual_heavy",
        "narrative_depth":     "standard",
        "max_sections":        12,
        "hero_sections":       ["drift_detection", "historical_comparison", "anomaly"],
        "expandable_sections": ["predictive_readiness", "forecast"],
        "description":         "Monitoring focus: drift detection, historical comparison, and trend hero.",
    },
}

# ── Intent Keyword Signals ────────────────────────────────────────────────────
# Each tuple: (phrase_to_match, style_vote, weight)
# Phrases matched as lowercased substrings of the full intent text.
# Scores accumulate; highest total wins.

_INTENT_SIGNALS: list[tuple[str, str, float]] = [
    # Executive / brief
    ("executive",        "executive_brief",    1.2),
    ("one page",         "executive_brief",    1.5),
    ("one-page",         "executive_brief",    1.5),
    ("brief",            "executive_brief",    1.0),
    ("summary only",     "executive_brief",    1.3),
    ("board",            "executive_brief",    1.1),
    ("c-suite",          "executive_brief",    1.2),
    ("high level",       "executive_brief",    1.1),
    ("high-level",       "executive_brief",    1.1),
    ("quick summary",    "executive_brief",    1.2),
    ("quick overview",   "executive_brief",    1.1),
    # Visual / dashboard
    ("dashboard",        "visual_dashboard",   1.5),
    ("visual",           "visual_dashboard",   1.2),
    ("charts only",      "visual_dashboard",   1.8),
    ("chart",            "visual_dashboard",   0.8),
    ("charts",           "visual_dashboard",   0.9),
    ("graph",            "visual_dashboard",   0.9),
    ("graphs",           "visual_dashboard",   0.9),
    ("visualization",    "visual_dashboard",   1.2),
    ("visualize",        "visual_dashboard",   1.0),
    # Analyst / deep-dive
    ("detailed",         "analyst_deep_dive",  1.2),
    ("deep dive",        "analyst_deep_dive",  1.5),
    ("deep-dive",        "analyst_deep_dive",  1.5),
    ("full analysis",    "analyst_deep_dive",  1.4),
    ("full report",      "analyst_deep_dive",  1.2),
    ("comprehensive",    "analyst_deep_dive",  1.1),
    ("in-depth",         "analyst_deep_dive",  1.3),
    ("in depth",         "analyst_deep_dive",  1.3),
    ("complete report",  "analyst_deep_dive",  1.2),
    # Table-heavy
    ("table-heavy",      "table_heavy_report", 1.8),
    ("table heavy",      "table_heavy_report", 1.8),
    ("tables",           "table_heavy_report", 1.2),
    ("tabular",          "table_heavy_report", 1.3),
    ("drilldown",        "table_heavy_report", 1.2),
    ("drill down",       "table_heavy_report", 1.2),
    ("drill-down",       "table_heavy_report", 1.2),
    ("breakdown",        "table_heavy_report", 0.8),
    # Operational
    ("operational",      "operational_report", 1.5),
    ("operations",       "operational_report", 1.3),
    ("ops report",       "operational_report", 1.4),
    ("daily digest",     "operational_report", 1.4),
    ("daily report",     "operational_report", 1.1),
    ("ops",              "operational_report", 0.9),
    # Anomaly / quality
    ("anomaly",          "anomaly_report",     1.5),
    ("anomalies",        "anomaly_report",     1.5),
    ("data quality",     "anomaly_report",     1.4),
    ("quality report",   "anomaly_report",     1.4),
    ("missing values",   "anomaly_report",     1.2),
    ("outliers",         "anomaly_report",     1.2),
    ("outlier",          "anomaly_report",     1.2),
    ("issues",           "anomaly_report",     0.8),
    # KPI
    ("kpi focused",      "kpi_summary",        1.8),
    ("kpi only",         "kpi_summary",        1.8),
    ("kpi summary",      "kpi_summary",        1.8),
    ("kpis",             "kpi_summary",        1.5),
    ("kpi",              "kpi_summary",        1.3),
    ("metrics only",     "kpi_summary",        1.5),
    ("key metrics",      "kpi_summary",        1.2),
    # Monitoring / drift / comparison
    ("drift",            "monitoring_report",  1.5),
    ("drift detection",  "monitoring_report",  1.8),
    ("monitor",          "monitoring_report",  1.3),
    ("monitoring",       "monitoring_report",  1.5),
    ("tracking",         "monitoring_report",  1.0),
    ("comparison",       "monitoring_report",  1.0),
    ("compare",          "monitoring_report",  0.8),
    ("historical",       "monitoring_report",  1.2),
    ("week over week",   "monitoring_report",  1.4),
    ("month over month", "monitoring_report",  1.4),
]


# ── Dataset Signal Detection ───────────────────────────────────────────────────

def _detect_dataset_signals(
    semantic_profile: list[dict],
    date_profile: dict,
    numeric_profile: dict,
    categorical_profile: dict,
) -> dict:
    """Derive dataset type signals from stored profiles. Never raises."""
    try:
        groups: dict[str, int] = {}
        types: dict[str, int] = {}
        for s in (semantic_profile or []):
            g = s.get("semantic_group", "unknown")
            t = s.get("semantic_type", "unknown")
            groups[g] = groups.get(g, 0) + 1
            types[t] = types.get(t, 0) + 1

        date_cols      = (date_profile or {}).get("date_columns") or []
        trend_insights = (date_profile or {}).get("trend_insights") or []
        num_count      = len(numeric_profile or {})
        cat_count      = len(categorical_profile or {})
        total_cols     = num_count + cat_count

        # Infer dataset type label for metadata
        has_financial  = groups.get("financial_metric", 0) > 0
        has_dimensions = groups.get("dimension", 0) > 0
        has_time       = len(date_cols) > 0 and len(trend_insights) > 0

        if has_financial and has_dimensions and has_time:
            dataset_type = "sales_financial"
        elif has_financial and has_dimensions:
            dataset_type = "sales_data"
        elif has_financial:
            dataset_type = "financial_data"
        elif has_time and not has_financial:
            dataset_type = "time_series_log"
        elif groups.get("operational_metric", 0) > 0:
            dataset_type = "operational_data"
        elif groups.get("dimension", 0) >= 3:
            dataset_type = "segmented_data"
        else:
            dataset_type = "general_data"

        return {
            "dataset_type":           dataset_type,
            "has_financial":          has_financial,
            "has_dimensions":         has_dimensions,
            "has_time_series":        has_time,
            "has_date_column":        len(date_cols) > 0,
            "has_operational":        groups.get("operational_metric", 0) > 0,
            "has_segmentation":       groups.get("dimension", 0) >= 2,
            "is_numeric_heavy":       total_cols > 0 and num_count / total_cols > 0.6,
            "is_categorical_heavy":   total_cols > 0 and cat_count / total_cols > 0.6,
            "financial_col_count":    groups.get("financial_metric", 0),
            "dimension_col_count":    groups.get("dimension", 0),
            "semantic_groups":        groups,
            "semantic_types":         types,
        }
    except Exception:
        return {
            "dataset_type":         "general_data",
            "has_financial":        False,
            "has_dimensions":       False,
            "has_time_series":      False,
            "has_date_column":      False,
            "has_operational":      False,
            "has_segmentation":     False,
            "is_numeric_heavy":     False,
            "is_categorical_heavy": False,
            "financial_col_count":  0,
            "dimension_col_count":  0,
            "semantic_groups":      {},
            "semantic_types":       {},
        }


# ── User Preference Detection ─────────────────────────────────────────────────

def detect_user_preferences(intent_text: str) -> dict:
    """Detect user style preferences from intent text via keyword heuristics.

    Returns style_votes (accumulated scores per style), detected_keywords,
    inferred_audience, detail_hint, and visual_hint.
    No LLM. No ML. Deterministic substring matching only.
    """
    if not intent_text or not isinstance(intent_text, str):
        return {
            "style_votes":       {},
            "detected_keywords": [],
            "inferred_audience": "general",
            "detail_hint":       "medium",
            "visual_hint":       "balanced",
        }

    text = intent_text.lower()
    style_votes: dict[str, float] = {}
    detected_keywords: list[str] = []

    for phrase, style, weight in _INTENT_SIGNALS:
        if phrase in text:
            style_votes[style] = style_votes.get(style, 0.0) + weight
            detected_keywords.append(phrase)

    # Audience
    inferred_audience = "general"
    if any(k in text for k in ("executive", "ceo", "cfo", "board", "c-suite", "one page", "brief")):
        inferred_audience = "executive"
    elif any(k in text for k in ("analyst", "detailed", "deep dive", "in-depth", "in depth", "comprehensive", "full")):
        inferred_audience = "analyst"
    elif any(k in text for k in ("operations", "operational", "ops", "daily", "monitoring", "monitor")):
        inferred_audience = "operations"

    # Detail hint
    detail_hint = "medium"
    if any(k in text for k in ("brief", "summary", "quick", "one page", "one-page")):
        detail_hint = "low"
    elif any(k in text for k in ("detailed", "full", "comprehensive", "in-depth", "complete", "everything")):
        detail_hint = "high"

    # Visual hint
    visual_hint = "balanced"
    if any(k in text for k in ("chart", "charts", "visual", "dashboard", "graph", "graphs")):
        visual_hint = "visual_heavy"
    elif any(k in text for k in ("table", "tables", "tabular", "drilldown")):
        visual_hint = "table_heavy"
    elif any(k in text for k in ("kpi", "kpis", "metrics only", "key metrics")):
        visual_hint = "kpi_only"

    return {
        "style_votes":       style_votes,
        "detected_keywords": list(set(detected_keywords)),
        "inferred_audience": inferred_audience,
        "detail_hint":       detail_hint,
        "visual_hint":       visual_hint,
    }


def _style_from_dataset(signals: dict) -> str | None:
    """Suggest a style from dataset characteristics alone when intent is absent."""
    if signals.get("has_time_series") and not signals.get("has_financial"):
        return "monitoring_report"
    if signals.get("has_financial") and signals.get("has_dimensions") and signals.get("has_time_series"):
        return "analyst_deep_dive"
    if signals.get("has_financial") and signals.get("has_segmentation"):
        return "analyst_deep_dive"
    if signals.get("has_financial") and not signals.get("has_dimensions"):
        return "kpi_summary"
    if signals.get("is_categorical_heavy") and signals.get("has_segmentation"):
        return "table_heavy_report"
    return None


def infer_report_style(
    intent_text: str | None,
    semantic_profile: list[dict],
    date_profile: dict,
    numeric_profile: dict,
    categorical_profile: dict,
) -> tuple[str, dict]:
    """Infer report style from intent text and dataset signals.

    Returns (style_name, preferences_dict).
    Precedence: strong intent keywords > weak intent keywords > dataset signals > default.
    Never raises.
    """
    try:
        prefs   = detect_user_preferences(intent_text or "")
        signals = _detect_dataset_signals(
            semantic_profile, date_profile, numeric_profile, categorical_profile
        )

        if prefs["style_votes"]:
            best_style = max(prefs["style_votes"], key=lambda k: prefs["style_votes"][k])
            best_score = prefs["style_votes"][best_style]
            if best_score >= 1.0:
                return best_style, prefs

        dataset_style = _style_from_dataset(signals)
        if dataset_style:
            return dataset_style, prefs

        return _DEFAULT_STYLE, prefs

    except Exception:
        return _DEFAULT_STYLE, {}


# ── Section Reorder Helper ────────────────────────────────────────────────────

def reorder_sections(sections: list[dict], section_scores: dict[str, int]) -> list[dict]:
    """Stable-sort sections by descending score from section_scores.

    Sections with the same score preserve their original relative order.
    Sections whose type is absent from section_scores get score 0 (go last).
    """
    if not section_scores or not sections:
        return sections
    return sorted(
        sections,
        key=lambda s: section_scores.get(s.get("type", "text"), 0),
        reverse=True,
    )


# ── Main Planner Entry Point ───────────────────────────────────────────────────

def plan_report(
    intent_text: str | None,
    semantic_profile: list[dict],
    date_profile: dict,
    numeric_profile: dict,
    categorical_profile: dict,
    available_section_types: list[str] | None = None,
) -> dict:
    """Build a ReportPlan from intent text and dataset profiles.

    Output fields:
      report_style          — one of 8 style names
      audience              — executive | analyst | operations | general
      detail_level          — low | medium | high
      visual_preference     — visual_heavy | table_heavy | balanced | kpi_only
      narrative_depth       — minimal | standard | detailed
      section_order         — list of section types sorted high-priority-first
      section_scores        — dict of type → int score
      viz_type_scores       — dict of viz type → int score
      viz_selection_rules   — deterministic rules for chart type selection
      detected_preferences  — list of matched intent keyword phrases
      dataset_signals       — derived dataset characteristics dict
      style_description     — human-readable description of chosen style
      layout_metadata       — hero_sections, expandable_sections, max_sections
      future_hooks          — integration points for later phases

    Never raises. Returns a valid fallback plan on any error.
    """
    try:
        style, prefs = infer_report_style(
            intent_text, semantic_profile, date_profile,
            numeric_profile, categorical_profile,
        )
        signals  = _detect_dataset_signals(
            semantic_profile, date_profile, numeric_profile, categorical_profile
        )
        meta     = _STYLE_META.get(style, _STYLE_META[_DEFAULT_STYLE])
        scores   = _SECTION_SCORES.get(style, _SECTION_SCORES[_DEFAULT_STYLE])
        viz      = _VIZ_SCORES.get(style, _VIZ_SCORES[_DEFAULT_STYLE])

        # Build ordered section list from what is actually present in this report
        if available_section_types:
            section_order = sorted(
                available_section_types,
                key=lambda t: scores.get(t, 0),
                reverse=True,
            )
        else:
            # Fall back to all types in the style's score dict, priority-sorted
            section_order = sorted(scores.keys(), key=lambda t: scores[t], reverse=True)

        return {
            "report_style":         style,
            "audience":             meta["audience"],
            "detail_level":         meta["detail_level"],
            "visual_preference":    meta["visual_preference"],
            "narrative_depth":      meta["narrative_depth"],
            "section_order":        section_order,
            "section_scores":       scores,
            "viz_type_scores":      viz,
            "viz_selection_rules":  VIZ_SELECTION_RULES,
            "detected_preferences": prefs.get("detected_keywords", []),
            "dataset_signals":      signals,
            "style_description":    meta.get("description", ""),
            "layout_metadata": {
                "max_sections":        meta["max_sections"],
                "hero_sections":       meta["hero_sections"],
                "expandable_sections": meta["expandable_sections"],
            },
            # Future integration hooks
            "future_hooks": {
                "conversational_analytics":  "Pass report_plan.audience to conversational layer",
                "ai_narrative_tone":         "Pass report_plan.narrative_depth to AI summary prompt",
                "adaptive_ui_rendering":     "Pass report_plan.layout_metadata to frontend section renderer",
                "realtime_dashboard":        "Pass report_plan.viz_type_scores to chart selector",
                "ml_recommendations":        "Pass report_plan.dataset_signals to ML feature selector",
                "autonomous_monitoring":     "Use report_plan.report_style == monitoring_report to trigger alerts",
            },
        }

    except Exception:
        return {
            "report_style":         _DEFAULT_STYLE,
            "audience":             "analyst",
            "detail_level":         "high",
            "visual_preference":    "balanced",
            "narrative_depth":      "detailed",
            "section_order":        [],
            "section_scores":       _SECTION_SCORES.get(_DEFAULT_STYLE, {}),
            "viz_type_scores":      _VIZ_SCORES.get(_DEFAULT_STYLE, {}),
            "viz_selection_rules":  VIZ_SELECTION_RULES,
            "detected_preferences": [],
            "dataset_signals":      {},
            "style_description":    "Full-detail report (fallback).",
            "layout_metadata": {
                "max_sections":        20,
                "hero_sections":       [],
                "expandable_sections": [],
            },
            "future_hooks": {},
        }
