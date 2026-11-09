"""
Data Quality Intelligence — Phase 1C.

Pure in-memory computation from existing ColumnProfile statistics.
No SQL, no DB access, no side effects except updating cp in-place via
enrich_column_quality().

Dimensions implemented (deterministic only, no LLM):
  1. Completeness  — completeness_score (0-100)
  2. Consistency   — format_consistency_score (0-100)
  3. Validity      — valid_count, invalid_count, invalid_percentage, validation_status
  4. Uniqueness    — duplicate_count, duplicate_percentage (in-memory, not persisted)
  5. Quality Score — quality_score (0-100), quality_grade (A/B/C/D/F)
  6. Quality Summary — quality_summary_json {strengths, issues, recommendations}
"""
from __future__ import annotations

import json


# ── Grade thresholds (descending) ─────────────────────────────────────────────

_GRADE_THRESHOLDS: list[tuple[float, str]] = [
    (90.0, 'A'),
    (75.0, 'B'),
    (60.0, 'C'),
    (40.0, 'D'),
]


def _grade(score: float) -> str:
    for threshold, letter in _GRADE_THRESHOLDS:
        if score >= threshold:
            return letter
    return 'F'


# ── 1. Completeness ───────────────────────────────────────────────────────────

def compute_column_completeness_score(
    null_percentage: float | None,
    blank_percentage: float | None,
) -> float | None:
    """Column completeness score 0-100.

    = 100 - null_pct - blank_pct (capped at 0).
    Returns None when null stats are not yet computed (structural-only pass).
    blank_percentage is always a subset of populated rows, so the result
    is always non-negative in practice; max(0,.) guards against rounding.
    """
    if null_percentage is None:
        return None
    blank_pct = blank_percentage or 0.0
    return round(max(0.0, 100.0 - null_percentage - blank_pct), 2)


# ── 2. Consistency ────────────────────────────────────────────────────────────

def compute_format_consistency_score(
    pattern_coverage: float | None,
) -> float | None:
    """Format consistency score 0-100.

    Identical to pattern_coverage (the dominant format's % of non-empty rows).
    Returns None when pattern detection has not run (structural-only or PII cols).
    Mixed-format is indicated by a score < 50.
    """
    if pattern_coverage is None:
        return None
    return round(pattern_coverage, 2)


# ── 3. Validity ───────────────────────────────────────────────────────────────

def _resolve_validator_rate(
    semantic_type_value: str | None,
    email_match_rate: float | None,
    phone_match_rate: float | None,
    guid_match_rate: float | None,
    date_string_rate: float | None,
) -> float | None:
    """Return the applicable format-match rate for the column, or None.

    Priority: semantic-type-aware match (EMAIL/PHONE/ID), then high-confidence
    GUID or date-string heuristics.  Returns None when no deterministic
    validator applies — validity is then 'unknown', not penalised.
    """
    if semantic_type_value == 'EMAIL' and email_match_rate is not None:
        return email_match_rate
    if semantic_type_value == 'PHONE' and phone_match_rate is not None:
        return phone_match_rate
    if semantic_type_value == 'ID' and guid_match_rate is not None:
        return guid_match_rate
    # High-confidence GUID heuristic: majority of non-null values are GUIDs
    if guid_match_rate is not None and guid_match_rate >= 50.0:
        return guid_match_rate
    # High-confidence date-string heuristic for TEXT columns
    if date_string_rate is not None and date_string_rate >= 80.0:
        return date_string_rate
    return None


def compute_validity_metrics(
    semantic_type_value: str | None,
    populated_count: int | None,
    email_match_rate: float | None,
    phone_match_rate: float | None,
    guid_match_rate: float | None,
    date_string_rate: float | None,
) -> tuple[int | None, int | None, float | None, str]:
    """Return (valid_count, invalid_count, invalid_percentage, validation_status).

    validation_status values:
      'valid'           — ≥ 95% of populated values pass the validator
      'partially_valid' — 50–94% pass
      'invalid'         — < 50% pass
      'unknown'         — no deterministic validator applies to this column
    """
    rate = _resolve_validator_rate(
        semantic_type_value,
        email_match_rate, phone_match_rate,
        guid_match_rate, date_string_rate,
    )

    if rate is None or populated_count is None:
        return None, None, None, 'unknown'

    invalid_pct = round(max(0.0, 100.0 - rate), 2)
    valid_cnt   = round(populated_count * rate / 100)
    invalid_cnt = populated_count - valid_cnt

    if rate >= 95.0:
        status = 'valid'
    elif rate >= 50.0:
        status = 'partially_valid'
    else:
        status = 'invalid'

    return valid_cnt, invalid_cnt, invalid_pct, status


# ── 4. Uniqueness — derived, NOT persisted (like variance) ────────────────────

def compute_duplicate_metrics(
    populated_count: int | None,
    distinct_count: int | None,
) -> tuple[int | None, float | None]:
    """Return (duplicate_count, duplicate_percentage).

    Reuses distinct_count and populated_count — no new SQL issued.
    Not written to DB (same convention as ColumnProfile.variance).
    """
    if populated_count is None or distinct_count is None:
        return None, None
    dup_count = max(0, populated_count - distinct_count)
    dup_pct   = round((dup_count / populated_count) * 100.0, 2) if populated_count > 0 else 0.0
    return dup_count, dup_pct


# ── 5. Quality score ──────────────────────────────────────────────────────────

def compute_quality_score(
    completeness: float | None,
    consistency: float | None,
    validity: float | None,
) -> float | None:
    """Weighted mean over available quality dimensions (all on 0-100 scale).

    Nominal weights: completeness=0.40, consistency=0.30, validity=0.30.
    When a dimension is unavailable (None), its weight redistributes among
    the remaining components, so the score always uses the full 0-100 range.
    Returns None when no dimension is computable (structural-only + no patterns).
    """
    components: list[tuple[float, float]] = []

    if completeness is not None:
        components.append((completeness, 0.40))
    if consistency is not None:
        components.append((consistency, 0.30))
    if validity is not None:
        components.append((validity, 0.30))

    if not components:
        return None

    weight_sum = sum(w for _, w in components)
    score = sum(v * w for v, w in components) / weight_sum
    return round(min(100.0, max(0.0, score)), 2)


# ── 6. Quality summary ────────────────────────────────────────────────────────

def _build_quality_summary(
    null_percentage: float | None,
    blank_percentage: float | None,
    uniqueness_score: float | None,
    populated_count: int | None,
    pattern_coverage: float | None,
    validation_status: str | None,
    invalid_percentage: float | None,
) -> dict:
    """Return deterministic {strengths, issues, recommendations} from column stats."""
    strengths:       list[str] = []
    issues:          list[str] = []
    recommendations: list[str] = []

    null_pct  = null_percentage  or 0.0
    blank_pct = blank_percentage or 0.0

    # Completeness signals
    if null_percentage is not None:
        if null_pct == 0.0 and blank_pct == 0.0:
            strengths.append("No null or blank values")
        elif null_pct < 5.0 and blank_pct < 5.0:
            strengths.append("Very low null/blank rate (<5%)")
        if null_pct > 20.0:
            issues.append(f"High null rate ({null_pct:.1f}%)")
            recommendations.append("Investigate missing values at data source")
        if blank_pct > 5.0:
            issues.append(f"Elevated blank string rate ({blank_pct:.1f}%)")
            recommendations.append("Replace empty strings with NULL")

    # Uniqueness signals
    if uniqueness_score is not None:
        if uniqueness_score == 1.0:
            strengths.append("All populated values are unique")
        elif uniqueness_score < 0.1 and populated_count and populated_count > 100:
            issues.append(
                f"Very low uniqueness ({uniqueness_score:.0%}) — high duplication"
            )

    # Consistency signals
    if pattern_coverage is not None:
        if pattern_coverage >= 95.0:
            strengths.append("Highly consistent format")
        elif pattern_coverage < 50.0:
            issues.append(f"Mixed formats ({pattern_coverage:.0f}% dominant pattern)")
            recommendations.append("Standardize value format")

    # Validity signals
    if validation_status == 'valid':
        strengths.append("Values pass format validation")
    elif validation_status == 'invalid' and invalid_percentage is not None:
        issues.append(f"{invalid_percentage:.1f}% of values fail format validation")
        recommendations.append("Cleanse invalid values at the source system")
    elif validation_status == 'partially_valid' and invalid_percentage is not None:
        issues.append(f"{invalid_percentage:.1f}% of values fail validation checks")
        recommendations.append("Review and correct invalid values")

    return {"strengths": strengths, "issues": issues, "recommendations": recommendations}


# ── Orchestrator ──────────────────────────────────────────────────────────────

def enrich_column_quality(cp) -> None:
    """Compute and attach all Phase 1C quality metrics to a ColumnProfile in-place.

    Reads only from already-computed ColumnProfile fields — no SQL, no imports
    of DB modules.  Safe to call in STRUCTURAL_ONLY mode; most metrics will be
    None or 'unknown' when statistical fields are absent.

    Must be called AFTER profile_column_statistics, profile_sample_values,
    and _classify_columns so that null_percentage, match rates, and
    semantic_type are all populated.
    """
    # ── Resolve semantic type string ─────────────────────────────────────────
    st_val = None
    if cp.semantic_type is not None:
        st_val = (
            cp.semantic_type.value
            if hasattr(cp.semantic_type, 'value')
            else str(cp.semantic_type)
        )

    # ── 1. Completeness ──────────────────────────────────────────────────────
    cp.completeness_score = compute_column_completeness_score(
        cp.null_percentage, cp.blank_percentage,
    )

    # ── 2. Consistency ───────────────────────────────────────────────────────
    cp.format_consistency_score = compute_format_consistency_score(cp.pattern_coverage)

    # ── 3. Validity ──────────────────────────────────────────────────────────
    (
        cp.valid_count,
        cp.invalid_count,
        cp.invalid_percentage,
        cp.validation_status,
    ) = compute_validity_metrics(
        st_val, cp.populated_count,
        cp.email_match_rate, cp.phone_match_rate,
        cp.guid_match_rate,  cp.date_string_rate,
    )

    # ── 4. Uniqueness — derived in-memory, not persisted ────────────────────
    cp.duplicate_count, cp.duplicate_percentage = compute_duplicate_metrics(
        cp.populated_count, cp.distinct_count,
    )

    # ── 5. Dominant value — in-memory from top_values, not persisted ─────────
    cp.dominant_value_percentage = (
        cp.top_values[0].get('percentage') if cp.top_values else None
    )

    # ── 6. Quality score ─────────────────────────────────────────────────────
    # validity_component: 100 - invalid_pct (or None when validation is unknown)
    validity_component: float | None = None
    if cp.validation_status != 'unknown' and cp.invalid_percentage is not None:
        validity_component = max(0.0, 100.0 - cp.invalid_percentage)

    cp.quality_score = compute_quality_score(
        cp.completeness_score,
        cp.format_consistency_score,
        validity_component,
    )

    # ── 7. Quality grade ─────────────────────────────────────────────────────
    cp.quality_grade = _grade(cp.quality_score) if cp.quality_score is not None else None

    # ── 8. Quality summary ───────────────────────────────────────────────────
    cp.quality_summary_json = json.dumps(
        _build_quality_summary(
            null_percentage=cp.null_percentage,
            blank_percentage=cp.blank_percentage,
            uniqueness_score=cp.uniqueness_score,
            populated_count=cp.populated_count,
            pattern_coverage=cp.pattern_coverage,
            validation_status=cp.validation_status,
            invalid_percentage=cp.invalid_percentage,
        )
    )
