"""
core/intelligence/insight_explanation_builder.py

Deterministic insight explanation and root-cause analysis builder for ToolSmithAI.

Two public functions:

  build_insight_explanation(finding, semantic_labels, business_meanings)
      Returns {"title": "...", "why": [...]}
      Derives a structured WHY from the finding's own fields.

  build_root_cause_analysis(finding, semantic_labels, business_meanings,
                             numeric_profile, categorical_profile,
                             categorical_meta, missing_values, date_profile,
                             correlation_profile, row_count, column_count)
      Returns {"title": "...", "why": [...], "drivers": [...]}
      Extends build_insight_explanation with evidence-backed root-cause drivers
      derived from the stored dataset profile.

Rules:
  - No LLM dependency.
  - No raw data access.
  - Never invent causes — only report what profile data supports.
  - If evidence is insufficient, drivers is an empty list.
  - Deterministic: same inputs always produce the same output.

Usage:
    from core.intelligence.insight_explanation_builder import (
        build_insight_explanation,
        build_root_cause_analysis,
    )

    explanation = build_insight_explanation(finding, semantic_labels=lm, business_meanings=bm)
    rca         = build_root_cause_analysis(finding, semantic_labels=lm, business_meanings=bm,
                                             numeric_profile=np_, ...)
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

# ── Local formatting helper ────────────────────────────────────────────────────

def _safe_fmt(n, decimals: int = 2) -> str:
    """Format a numeric value as a comma-separated string. Returns '—' on failure."""
    if n is None:
        return "—"
    try:
        v = float(n)
        if not math.isfinite(v):
            return "—"
        formatted = f"{v:,.{decimals}f}"
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
        return formatted
    except (TypeError, ValueError):
        return str(n)


# ── Impact statements keyed by (category, severity) ──────────────────────────
# Second "why" bullet for anomaly findings.
# Missing (category, severity) pairs fall back to (category, "medium").

_ANOMALY_IMPACT: dict[tuple[str, str], str] = {
    ("missing_data", "high"):   (
        "Missing data at this rate distorts aggregate statistics "
        "and may invalidate analysis conclusions."
    ),
    ("missing_data", "medium"): (
        "Missing values reduce the effective sample size "
        "and may introduce bias in numeric summaries."
    ),
    ("missing_data", "low"):    (
        "Low-level missing data has minimal immediate impact "
        "but should be monitored."
    ),
    ("distribution", "high"):   (
        "Extreme distribution patterns indicate data quality issues "
        "or structural anomalies requiring investigation."
    ),
    ("distribution", "medium"): (
        "Skewed or irregular distributions may bias statistical summaries "
        "and downstream analysis."
    ),
    ("distribution", "low"):    (
        "Minor distribution irregularity — within acceptable bounds."
    ),
    ("trend", "high"):          (
        "Sharp trend shifts signal structural breaks "
        "that invalidate period-over-period comparisons."
    ),
    ("trend", "medium"):        (
        "Significant trend movement warrants investigation "
        "for external drivers or data collection changes."
    ),
    ("sample_size", "high"):    (
        "With fewer than 30 records, no statistical finding "
        "can be considered reliable."
    ),
    ("sample_size", "medium"):  (
        "A sample below 100 records limits the generalizability "
        "of any observed pattern."
    ),
    ("quality", "high"):        (
        "Data quality issues at this severity directly undermine "
        "analytical trust and reproducibility."
    ),
    ("quality", "medium"):      (
        "Near-duplicate or correlated fields reduce analytical power "
        "and may complicate modeling."
    ),
    ("quality", "low"):         (
        "Minor quality concern — monitor but no immediate action required."
    ),
}

# ── Context statements keyed by recommendation action_type ────────────────────
_RECOMMENDATION_CONTEXT: dict[str, str] = {
    "clean_data": (
        "Unresolved missing data propagates errors through all downstream "
        "analysis and reporting."
    ),
    "review":     (
        "Validating this finding ensures analysis conclusions are grounded "
        "in reliable data."
    ),
    "segment":    (
        "Segment-level analysis reveals patterns invisible in aggregate summaries."
    ),
    "schedule":   (
        "Automated monitoring prevents data drift from going undetected "
        "between manual reviews."
    ),
}

# ── Category → business-framed title fragment ─────────────────────────────────
_CATEGORY_TITLE: dict[str, str] = {
    "missing_data": "Data Completeness Risk",
    "distribution": "Distribution Anomaly",
    "trend":        "Trend Risk Detected",
    "sample_size":  "Sample Size Limitation",
    "quality":      "Data Quality Concern",
}

# Anomaly titles that do NOT reference a specific column — skip column prefix
_TITLE_NO_COLUMN_MARKERS = frozenset({
    "no major", "low dataset", "small sample", "very small", "all clear",
    "reduced dataset",
})


# ── Shared helpers ────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    """Normalise a column or label for fuzzy matching.

    Converts underscores, hyphens, and dots to spaces then lowercases,
    so display labels ("Sales Revenue") compare equal to raw names
    ("sales_revenue").
    """
    return s.replace("_", " ").replace("-", " ").replace(".", " ").lower()


def _extract_column_from_title(title: str) -> str | None:
    """Extract the column-name portion from an anomaly title.

    Pattern: "<Finding Type>: <Column Display Name>"
             "<Finding Type>: <Column A> & <Column B>"  (dual-column)

    Returns the text after the last ": " (first segment for dual-column),
    or None when no column reference is present.
    """
    if ": " not in title:
        return None
    try:
        part = title.split(": ", 1)[-1]
        return part.split(" & ")[0].strip() or None
    except Exception:
        return None


def _extract_two_columns_from_title(title: str) -> tuple[str | None, str | None]:
    """Extract two column hints from a dual-column anomaly title.

    Pattern: "Finding Type: Column A & Column B"
    """
    if ": " not in title or " & " not in title:
        return None, None
    try:
        after_colon = title.split(": ", 1)[-1]
        parts       = after_colon.split(" & ", 1)
        return parts[0].strip() or None, parts[1].strip() or None
    except Exception:
        return None, None


def _resolve_business_meaning(
    col_hint: str | None,
    business_meanings: dict[str, str],
) -> str | None:
    """Return the enterprise business meaning for a column hint, or None.

    Resolution order:
      1. Exact raw-column key match.
      2. Normalised equality (display label ↔ raw key).
      3. Normalised substring match.
    """
    if not col_hint or not business_meanings:
        return None

    if col_hint in business_meanings:
        return business_meanings[col_hint]

    hint_norm = _norm(col_hint)

    for raw_col, meaning in business_meanings.items():
        if hint_norm == _norm(raw_col):
            return meaning

    for raw_col, meaning in business_meanings.items():
        raw_norm = _norm(raw_col)
        if hint_norm in raw_norm or raw_norm in hint_norm:
            return meaning

    return None


def _find_raw_column(
    display_hint: str | None,
    label_map: dict[str, str],
) -> str | None:
    """Reverse-resolve a display label to its raw column name.

    Handles three cases:
      1. display_hint is already a raw column name (key in label_map).
      2. display_hint is a display label (value in label_map).
      3. Normalised match (underscore vs space, case differences).
    """
    if not display_hint or not label_map:
        return None

    # Case 1: already a raw column name
    if display_hint in label_map:
        return display_hint

    # Case 2: exact display-label match
    for raw_col, label in label_map.items():
        if label == display_hint:
            return raw_col

    # Case 3: normalised match
    hint_norm = _norm(display_hint)
    for raw_col, label in label_map.items():
        if _norm(label) == hint_norm or _norm(raw_col) == hint_norm:
            return raw_col

    return None


# ── Public: Insight Explanation (WHY) ────────────────────────────────────────

def build_insight_explanation(
    finding: dict,
    semantic_labels: dict | None = None,
    business_meanings: dict | None = None,
) -> dict:
    """Build a structured WHY explanation for an anomaly or recommendation.

    Inputs:
        finding:          Anomaly dict (category / severity / evidence) or
                          recommendation dict (action_type / reason / priority).
        semantic_labels:  label_map from build_label_map() — col → display name.
        business_meanings: business_meaning_map — col → enterprise concept string.

    Returns:
        {"title": "<business-framed title>", "why": ["<reason 1>", ...]}

    Never raises. Returns a minimal fallback on any error.
    """
    try:
        bm = business_meanings or {}
        is_recommendation = "action_type" in finding
        raw_title = finding.get("title", "")

        col_hint    = _extract_column_from_title(raw_title)
        col_meaning = _resolve_business_meaning(col_hint, bm)

        # ── Title ─────────────────────────────────────────────────────────────
        if is_recommendation:
            expl_title = raw_title
        else:
            category   = finding.get("category", "")
            base_title = _CATEGORY_TITLE.get(category, raw_title)
            title_lower = raw_title.lower()
            has_no_col  = any(m in title_lower for m in _TITLE_NO_COLUMN_MARKERS)
            if col_meaning and not has_no_col:
                expl_title = f"{col_meaning}: {base_title}"
            else:
                expl_title = raw_title

        # ── Why bullets ───────────────────────────────────────────────────────
        why: list[str] = []

        if is_recommendation:
            reason = finding.get("reason", "").strip()
            if reason:
                why.append(reason)
            action_ctx = _RECOMMENDATION_CONTEXT.get(finding.get("action_type", ""), "")
            if action_ctx and action_ctx not in why:
                why.append(action_ctx)
            if col_meaning and len(why) < 3:
                why.append(f"Applies directly to {col_meaning}.")
        else:
            evidence = finding.get("evidence", "").strip()
            if evidence:
                why.append(evidence)
            category = finding.get("category", "")
            severity = finding.get("severity", "medium")
            impact   = _ANOMALY_IMPACT.get(
                (category, severity),
                _ANOMALY_IMPACT.get((category, "medium"), ""),
            )
            if impact and impact not in why:
                why.append(impact)
            if col_meaning and len(why) < 3:
                why.append(
                    f"{col_meaning} is a key business indicator — "
                    "data quality here directly affects business decisions."
                )

        if not why:
            desc = finding.get("description", "").strip()
            if desc:
                why.append(desc)

        return {
            "title": expl_title or raw_title or "Insight",
            "why":   why[:3],
        }

    except Exception:
        title = "Insight"
        try:
            title = finding.get("title", "Insight") if isinstance(finding, dict) else "Insight"
        except Exception:
            pass
        return {"title": title, "why": []}


# ── Root-cause driver builders (per anomaly category) ────────────────────────
# Each function returns a list of at most 2 evidence-backed driver statements.
# Rules enforced in every builder:
#   - Never invent causes.
#   - Only include a driver when the supporting data is present and finite.
#   - Return [] when evidence is absent or insufficient.

def _drivers_missing_data(
    raw_col: str | None,
    missing_values: dict,
    row_count: int,
    lm: dict,
    bm: dict,
) -> list[str]:
    drivers: list[str] = []
    if not missing_values:
        return drivers

    positive_missing = {
        col: float(cnt)
        for col, cnt in missing_values.items()
        if isinstance(cnt, (int, float)) and math.isfinite(float(cnt)) and cnt > 0
    }
    total_missing = sum(positive_missing.values())

    if raw_col and raw_col in positive_missing and total_missing > 0:
        share       = positive_missing[raw_col] / total_missing * 100
        col_display = bm.get(raw_col, lm.get(raw_col, raw_col))
        drivers.append(
            f"{col_display} accounts for {round(share, 1)}% "
            "of all missing cells in the dataset."
        )

    others = [col for col in positive_missing if col != raw_col]
    if others:
        n = len(others)
        if n <= 3:
            names = ", ".join(bm.get(c, lm.get(c, c)) for c in others)
            drivers.append(
                f"{n} other column{'s' if n > 1 else ''} also "
                f"{'have' if n > 1 else 'has'} missing values: {names}."
            )
        else:
            drivers.append(
                f"{n} other columns across the dataset also have missing data, "
                "compounding the overall completeness gap."
            )

    return drivers[:2]


def _drivers_distribution_numeric(
    raw_col: str | None,
    numeric_profile: dict,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Drivers for numeric distribution anomalies (outliers, range, variability)."""
    if not raw_col or raw_col not in numeric_profile:
        return []

    stats       = numeric_profile[raw_col]
    drivers: list[str] = []

    oc   = stats.get("outlier_count_iqr")
    nn   = stats.get("non_null_count")
    mn   = stats.get("min")
    mx   = stats.get("max")
    mean = stats.get("mean")
    std  = stats.get("std")

    # IQR outlier density
    if oc is not None and nn is not None:
        try:
            oc_f, nn_f = float(oc), float(nn)
            if nn_f > 0 and oc_f > 0:
                density = oc_f / nn_f * 100
                drivers.append(
                    f"{int(oc_f):,} of {int(nn_f):,} values "
                    f"({round(density, 1)}%) exceed the IQR outlier threshold."
                )
        except (TypeError, ValueError):
            pass

    # Value range spread
    if len(drivers) < 2 and mn is not None and mx is not None:
        try:
            mn_f, mx_f = float(mn), float(mx)
            if math.isfinite(mn_f) and math.isfinite(mx_f) and mn_f > 0:
                ratio = mx_f / mn_f
                if ratio >= 100:
                    drivers.append(
                        f"Value range: {_safe_fmt(mn_f)} to {_safe_fmt(mx_f)} "
                        f"({round(ratio):,}x spread from minimum to maximum)."
                    )
        except (TypeError, ValueError):
            pass

    # Coefficient of variation when no better signal was captured
    if len(drivers) < 2 and mean is not None and std is not None:
        try:
            mean_f, std_f = float(mean), float(std)
            if math.isfinite(mean_f) and math.isfinite(std_f) and mean_f != 0:
                cv = std_f / abs(mean_f)
                if cv > 1.0:
                    drivers.append(
                        f"Coefficient of variation = {round(cv, 2)} "
                        f"(std = {_safe_fmt(std_f)}, mean = {_safe_fmt(mean_f)}) "
                        "— high relative spread around the mean."
                    )
        except (TypeError, ValueError):
            pass

    return drivers[:2]


def _drivers_distribution_categorical(
    raw_col: str | None,
    categorical_profile: dict,
    categorical_meta: dict | None,
    row_count: int,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Drivers for categorical distribution anomalies (dominance, low entropy)."""
    if not raw_col:
        return []

    entries = categorical_profile.get(raw_col, [])
    meta    = (categorical_meta or {}).get(raw_col, {})
    drivers: list[str] = []

    if entries and row_count > 0:
        top     = entries[0]
        top_val = str(top.get("value", ""))
        top_cnt = top.get("count", 0)
        try:
            pct = round(float(top_cnt) / row_count * 100, 1)
            drivers.append(
                f'"{top_val}" alone accounts for {pct}% of all records '
                f"({int(top_cnt):,} of {row_count:,})."
            )
        except (TypeError, ValueError):
            pass

        if len(entries) >= 2 and len(drivers) < 2:
            second = entries[1]
            s_val  = str(second.get("value", ""))
            s_cnt  = second.get("count", 0)
            try:
                s_pct = round(float(s_cnt) / row_count * 100, 1)
                drivers.append(
                    f'Next most common value is "{s_val}" at {s_pct}% — '
                    "distribution is heavily concentrated toward the dominant value."
                )
            except (TypeError, ValueError):
                pass

    # Entropy-based insight when no entry data available
    if not drivers and meta:
        entropy = meta.get("entropy_approx")
        unique  = meta.get("unique_count", 0)
        if entropy is not None:
            try:
                drivers.append(
                    f"Entropy = {round(float(entropy), 3)} with {unique} "
                    f"unique value{'s' if unique != 1 else ''} — "
                    "near-zero diversity limits the segmentation value of this column."
                )
            except (TypeError, ValueError):
                pass

    return drivers[:2]


def _drivers_trend(
    raw_col: str | None,
    date_profile: dict,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Drivers for trend anomalies."""
    drivers: list[str] = []
    trend_insights      = date_profile.get("trend_insights") or []

    matching_ti: dict | None = None
    for ti in trend_insights:
        if ti.get("column") == raw_col:
            matching_ti = ti
            break

    if matching_ti:
        pct = matching_ti.get("pct_change")
        if pct is not None:
            try:
                pct_f     = float(pct)
                sign      = "+" if pct_f > 0 else ""
                direction = "increase" if pct_f > 0 else "decrease"
                drivers.append(
                    f"{sign}{round(pct_f, 1)}% {direction} from the first half to "
                    "the second half of the dataset (sorted chronologically)."
                )
            except (TypeError, ValueError):
                pass

    # Concurrent trend in another column
    concurrent = [
        ti for ti in trend_insights
        if ti.get("column") != raw_col and ti.get("pct_change") is not None
    ]
    if concurrent:
        other_ti  = concurrent[0]
        other_col = other_ti.get("column", "")
        other_pct = other_ti.get("pct_change", 0)
        try:
            other_pct_f = float(other_pct)
            sign        = "+" if other_pct_f >= 0 else ""
            col_display = bm.get(other_col, lm.get(other_col, other_col))
            drivers.append(
                f"Concurrent trend in {col_display}: "
                f"{sign}{round(other_pct_f, 1)}% change — "
                "multiple metrics moving together may signal a dataset-level structural shift."
            )
        except (TypeError, ValueError):
            pass

    return drivers[:2]


def _drivers_sample_size(
    row_count: int,
    column_count: int,
    severity: str,
) -> list[str]:
    """Drivers for sample size anomalies."""
    try:
        if int(column_count) <= 0:
            return []
    except (TypeError, ValueError):
        return []

    threshold = 30 if severity == "high" else 100
    record_word = "record is" if row_count == 1 else "records are"
    drivers: list[str] = [
        f"Statistical significance requires at least {threshold} records; "
        f"only {row_count:,} {record_word} present."
    ]
    if column_count > 0 and row_count > 0:
        ratio = round(row_count / column_count, 1)
        drivers.append(
            f"Data-to-feature ratio is {ratio:.1f}:1 "
            f"({row_count:,} rows, {column_count} columns) — "
            "multivariate patterns cannot be reliably estimated at this scale."
        )
    return drivers[:2]


def _drivers_quality_correlation(
    finding: dict,
    correlation_profile: list,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Drivers for data quality anomalies (near-perfect correlation / collinearity)."""
    drivers: list[str] = []
    if not correlation_profile:
        return drivers

    title    = finding.get("title", "")
    hint_a, hint_b = _extract_two_columns_from_title(title)

    for pair in correlation_profile:
        a, b   = pair.get("column_a", ""), pair.get("column_b", "")
        a_label = lm.get(a, a)
        b_label = lm.get(b, b)
        corr    = pair.get("correlation", 0)

        match_a = hint_a and (
            _norm(hint_a) in _norm(a_label) or _norm(a_label) in _norm(hint_a)
        )
        match_b = hint_b and (
            _norm(hint_b) in _norm(b_label) or _norm(b_label) in _norm(hint_b)
        )
        # Also try swapped columns
        if not (match_a and match_b):
            match_a = hint_a and (
                _norm(hint_a) in _norm(b_label) or _norm(b_label) in _norm(hint_a)
            )
            match_b = hint_b and (
                _norm(hint_b) in _norm(a_label) or _norm(a_label) in _norm(hint_b)
            )

        if match_a and match_b:
            try:
                c_f = float(corr)
                shared_pct = round(abs(c_f) * 100, 1)
                drivers.append(
                    f"Pearson r = {round(c_f, 3)}: {shared_pct}% of variance "
                    "is shared between these two fields."
                )
                a_meaning = bm.get(a, "")
                b_meaning = bm.get(b, "")
                if a_meaning and b_meaning:
                    drivers.append(
                        f"'{a_meaning}' and '{b_meaning}' may represent "
                        "overlapping business concepts — retaining both can inflate "
                        "significance in downstream models."
                    )
                elif a_meaning or b_meaning:
                    drivers.append(
                        f"High correlation with {a_meaning or b_meaning} — "
                        "these fields may be functionally redundant."
                    )
            except (TypeError, ValueError):
                pass
            break

    return drivers[:2]


def _drivers_for_anomaly(
    finding: dict,
    raw_col: str | None,
    numeric_profile: dict,
    categorical_profile: dict,
    categorical_meta: dict | None,
    missing_values: dict,
    date_profile: dict,
    correlation_profile: list,
    row_count: int,
    column_count: int,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Dispatch to the correct driver builder based on anomaly category."""
    category = finding.get("category", "")

    if category == "missing_data":
        return _drivers_missing_data(raw_col, missing_values, row_count, lm, bm)

    if category == "distribution":
        # Numeric or categorical?
        is_numeric_col = raw_col is not None and raw_col in numeric_profile
        is_cat_col     = raw_col is not None and (
            raw_col in categorical_profile
            or raw_col in (categorical_meta or {})
        )
        if is_numeric_col:
            return _drivers_distribution_numeric(raw_col, numeric_profile, lm, bm)
        if is_cat_col:
            return _drivers_distribution_categorical(
                raw_col, categorical_profile, categorical_meta, row_count, lm, bm
            )
        return []

    if category == "trend":
        return _drivers_trend(raw_col, date_profile, lm, bm)

    if category == "sample_size":
        return _drivers_sample_size(row_count, column_count, finding.get("severity", "medium"))

    if category == "quality":
        title = finding.get("title", "")
        if "Correlated" in title:
            return _drivers_quality_correlation(finding, correlation_profile, lm, bm)
        # Entropy collapse — categorical meta check
        if raw_col and categorical_meta and raw_col in categorical_meta:
            return _drivers_distribution_categorical(
                raw_col, categorical_profile, categorical_meta, row_count, lm, bm
            )
        return []

    return []


def _drivers_for_recommendation(
    finding: dict,
    numeric_profile: dict,
    categorical_profile: dict,
    missing_values: dict,
    date_profile: dict,
    correlation_profile: list,
    row_count: int,
    lm: dict,
    bm: dict,
) -> list[str]:
    """Build root-cause drivers for a recommendation finding."""
    action_type = finding.get("action_type", "")
    drivers: list[str] = []

    if action_type == "clean_data":
        sorted_missing = sorted(
            [
                (col, int(cnt))
                for col, cnt in missing_values.items()
                if isinstance(cnt, (int, float))
                and math.isfinite(float(cnt))
                and cnt > 0
            ],
            key=lambda x: -x[1],
        )
        if sorted_missing and row_count > 0:
            top_col, top_cnt = sorted_missing[0]
            rate        = round(top_cnt / row_count * 100, 1)
            col_display = bm.get(top_col, lm.get(top_col, top_col))
            drivers.append(
                f"Highest impact: {col_display} is missing "
                f"{top_cnt:,} values ({rate}% of all rows)."
            )
        if len(sorted_missing) > 1:
            n = len(sorted_missing)
            drivers.append(
                f"Resolving the top {min(3, n)} affected column(s) "
                f"would recover the most analytical coverage across {n} total gap(s)."
            )

    elif action_type == "schedule":
        date_cols = date_profile.get("date_columns") or []
        if date_cols:
            dc          = date_cols[0]
            gran        = dc.get("inferred_granularity", "")
            days        = dc.get("range_days", 0)
            col         = dc.get("column", "")
            col_display = bm.get(col, lm.get(col, col))
            if gran and days:
                drivers.append(
                    f"{col_display} spans {days:,} days of {gran} data — "
                    "recurring analysis would automatically detect trend changes."
                )
            try:
                days_int = int(days)
                if days_int >= 365:
                    drivers.append(
                        "A full year of temporal data supports monthly, quarterly, "
                        "and year-over-year rollup reporting workflows."
                    )
                elif days_int >= 90:
                    drivers.append(
                        f"{days_int:,} days of data supports weekly and "
                        "monthly trend monitoring."
                    )
            except (TypeError, ValueError):
                pass

    elif action_type == "segment":
        if categorical_profile and row_count > 0:
            best = max(
                categorical_profile.items(),
                key=lambda x: len(x[1]),
                default=None,
            )
            if best:
                col, entries = best
                n_vals      = len(entries)
                col_display = bm.get(col, lm.get(col, col))
                if entries:
                    top_val = str(entries[0].get("value", ""))
                    try:
                        top_pct = round(entries[0].get("count", 0) / row_count * 100, 1)
                        drivers.append(
                            f"{col_display} has {n_vals} distinct value(s) — "
                            f'"{top_val}" leads at {top_pct}% of records.'
                        )
                    except (TypeError, ValueError):
                        drivers.append(
                            f"{col_display} has {n_vals} distinct value(s) "
                            "available for segment analysis."
                        )
        n_cat = len(categorical_profile)
        if n_cat > 1 and len(drivers) < 2:
            drivers.append(
                f"{n_cat} categorical dimensions are available — "
                "cross-segment filtering can surface hidden performance disparities."
            )

    elif action_type == "review":
        # Strongest correlation pair
        if correlation_profile:
            try:
                top_pair = max(
                    correlation_profile,
                    key=lambda p: abs(p.get("correlation", 0)),
                )
                corr_val = abs(top_pair.get("correlation", 0))
                if corr_val >= 0.50:
                    a   = top_pair.get("column_a", "")
                    b   = top_pair.get("column_b", "")
                    a_d = bm.get(a, lm.get(a, a))
                    b_d = bm.get(b, lm.get(b, b))
                    drivers.append(
                        f"Strongest detected correlation: {a_d} and {b_d} "
                        f"(r = {round(float(top_pair['correlation']), 2)})."
                    )
            except (ValueError, TypeError):
                pass

        # Highest outlier-density column
        if len(drivers) < 2:
            for col, stats in numeric_profile.items():
                oc = stats.get("outlier_count_iqr", 0)
                nn = stats.get("non_null_count", 0)
                try:
                    if oc and nn and float(oc) / float(nn) >= 0.10:
                        density     = round(float(oc) / float(nn) * 100, 1)
                        col_display = bm.get(col, lm.get(col, col))
                        drivers.append(
                            f"{col_display} has the highest outlier density: "
                            f"{density}% of values exceed the IQR threshold."
                        )
                        break
                except (TypeError, ValueError):
                    continue

    return drivers[:2]


# ── Public: Root-Cause Analysis (WHY + DRIVERS) ───────────────────────────────

def build_root_cause_analysis(
    finding: dict,
    semantic_labels: dict | None = None,
    business_meanings: dict | None = None,
    numeric_profile: dict | None = None,
    categorical_profile: dict | None = None,
    categorical_meta: dict | None = None,
    missing_values: dict | None = None,
    date_profile: dict | None = None,
    correlation_profile: list | None = None,
    row_count: int = 0,
    column_count: int = 0,
) -> dict:
    """Build a structured root-cause explanation for an anomaly or recommendation.

    Extends build_insight_explanation() by adding a 'drivers' list: a set of
    evidence-backed statements that explain WHY the finding exists, derived
    exclusively from the stored dataset profile.

    Rules:
    - Never invent causes. Only report what profile data directly supports.
    - If evidence is insufficient, drivers is an empty list (never fabricated).
    - Deterministic: same inputs always produce the same outputs.
    - No LLM calls. No raw data access.

    Inputs:
        finding:             Anomaly or recommendation dict.
        semantic_labels:     label_map from build_label_map().
        business_meanings:   business_meaning_map from build_business_meaning_map().
        numeric_profile:     col → numeric stats (min/max/mean/std/outlier_count_iqr/...).
        categorical_profile: col → top-value list [{value, count}].
        categorical_meta:    col → metadata {unique_count, entropy_approx, top_value_share}.
        missing_values:      col → null_count.
        date_profile:        {date_columns: [...], trend_insights: [...]}.
        correlation_profile: [{column_a, column_b, correlation}].
        row_count:           Total rows in the dataset.
        column_count:        Total columns in the dataset.

    Returns:
        {
            "title":   "<business-framed title>",
            "why":     ["<reason 1>", ...],    # 1-3 items from build_insight_explanation
            "drivers": ["<driver 1>", ...]     # 0-2 evidence-backed root causes
        }

    Never raises. Returns {title, why, drivers: []} on any error.
    """
    try:
        lm  = semantic_labels or {}
        bm  = business_meanings or {}
        np_ = numeric_profile or {}
        cp_ = categorical_profile or {}
        cm_ = categorical_meta or {}
        mv_ = missing_values or {}
        dp_ = date_profile or {}
        crp = correlation_profile or []

        # ── Base explanation (title + why) ────────────────────────────────────
        base = build_insight_explanation(finding, semantic_labels=lm, business_meanings=bm)

        # ── Resolve column to raw key for profile lookups ─────────────────────
        col_hint = _extract_column_from_title(finding.get("title", ""))
        raw_col  = _find_raw_column(col_hint, lm)

        # ── Build drivers ─────────────────────────────────────────────────────
        is_recommendation = "action_type" in finding

        if is_recommendation:
            drivers = _drivers_for_recommendation(
                finding, np_, cp_, mv_, dp_, crp, row_count, lm, bm
            )
        else:
            drivers = _drivers_for_anomaly(
                finding, raw_col,
                np_, cp_, cm_, mv_, dp_, crp,
                row_count, column_count,
                lm, bm,
            )

        return {
            "title":   base["title"],
            "why":     base["why"],
            "drivers": drivers,
        }

    except Exception:
        logger.exception(
            "build_root_cause_analysis failed; category=%r",
            finding.get("category") if isinstance(finding, dict) else type(finding).__name__,
        )
        # Safe fallback — delegates to build_insight_explanation which itself never raises
        try:
            base = build_insight_explanation(
                finding,
                semantic_labels=semantic_labels,
                business_meanings=business_meanings,
            )
            return {"title": base["title"], "why": base["why"], "drivers": []}
        except Exception:
            logger.exception("build_root_cause_analysis recovery fallback also failed")
            title = "Insight"
            try:
                title = finding.get("title", "Insight") if isinstance(finding, dict) else "Insight"
            except Exception:
                pass
            return {"title": title, "why": [], "drivers": []}
