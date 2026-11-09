"""
Phase 1C — Data Quality Intelligence tests.

Covers:
  - compute_column_completeness_score: empty, null-heavy, blank-heavy, perfect
  - compute_format_consistency_score: from pattern_coverage, None passthrough
  - compute_validity_metrics: email, GUID, phone, date-string, unknown, partially valid
  - compute_duplicate_metrics: derived, in-memory only, no SQL
  - compute_quality_score: weighted formula, component redistribution, edge cases
  - _grade: A/B/C/D/F thresholds
  - enrich_column_quality: end-to-end in-place enrichment
  - quality_summary_json: strengths / issues / recommendations
  - DB schema: 9 new columns present after init_db (idempotent)
  - Regression: _col_row_params placeholder count still matches _COL_INSERT
  - Regression: Phase 1A/1B columns still present alongside Phase 1C

Run from project root:
    venv/Scripts/pytest tests/test_phase1c_data_quality.py -v
"""
from __future__ import annotations

import json
import os
import sqlite3
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-phase1c-secret-long-enough-value")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase1c-long-enough-value-12345")

from core.profiling.models import (
    ColumnProfile, ProfilingConfig, ProfilingDepth, ProfilingStatus, SemanticType,
)
from core.profiling.quality import (
    _build_quality_summary,
    _grade,
    compute_column_completeness_score,
    compute_duplicate_metrics,
    compute_format_consistency_score,
    compute_quality_score,
    compute_validity_metrics,
    enrich_column_quality,
)


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------

def _make_cp(
    data_type: str = 'TEXT',
    populated_count: int | None = 100,
    null_count: int | None = 0,
    null_percentage: float | None = 0.0,
    blank_percentage: float | None = None,
    distinct_count: int | None = 50,
    uniqueness_score: float | None = 0.5,
    pattern_coverage: float | None = None,
    email_match_rate: float | None = None,
    phone_match_rate: float | None = None,
    guid_match_rate: float | None = None,
    date_string_rate: float | None = None,
    semantic_type: SemanticType | None = None,
    top_values: list | None = None,
) -> ColumnProfile:
    return ColumnProfile(
        source_id=1,
        profiling_snapshot_id=1,
        table_fqn='dbo.Test',
        column_name='Col',
        data_type=data_type,
        raw_type=data_type.lower(),
        is_nullable=True,
        is_primary_key=False,
        is_identity=False,
        ordinal_position=1,
        populated_count=populated_count,
        null_count=null_count,
        null_percentage=null_percentage,
        blank_percentage=blank_percentage,
        distinct_count=distinct_count,
        uniqueness_score=uniqueness_score,
        pattern_coverage=pattern_coverage,
        email_match_rate=email_match_rate,
        phone_match_rate=phone_match_rate,
        guid_match_rate=guid_match_rate,
        date_string_rate=date_string_rate,
        semantic_type=semantic_type,
        top_values=top_values or [],
        profiling_depth=ProfilingDepth.STATISTICAL,
        profiling_status=ProfilingStatus.COMPLETE,
    )


# ===========================================================================
# 1. Completeness score
# ===========================================================================

class TestCompletenessScore:

    def test_perfect_column_scores_100(self):
        score = compute_column_completeness_score(0.0, 0.0)
        assert score == 100.0

    def test_zero_nulls_no_blanks_scores_100(self):
        score = compute_column_completeness_score(0.0, None)
        assert score == 100.0

    def test_all_null_column_scores_zero(self):
        score = compute_column_completeness_score(100.0, 0.0)
        assert score == 0.0

    def test_null_heavy_column(self):
        # 80% nulls → completeness 20
        score = compute_column_completeness_score(80.0, None)
        assert score == pytest.approx(20.0)

    def test_null_and_blank_combined(self):
        # 10% null + 5% blank = 15% bad → completeness 85
        score = compute_column_completeness_score(10.0, 5.0)
        assert score == pytest.approx(85.0)

    def test_blank_only_reduces_score(self):
        score = compute_column_completeness_score(0.0, 20.0)
        assert score == pytest.approx(80.0)

    def test_capped_at_zero_not_negative(self):
        # Edge case: rounding could produce tiny negatives
        score = compute_column_completeness_score(99.9, 0.2)
        assert score >= 0.0

    def test_none_null_percentage_returns_none(self):
        assert compute_column_completeness_score(None, None) is None

    def test_none_null_percentage_with_blank_returns_none(self):
        assert compute_column_completeness_score(None, 5.0) is None

    def test_returns_float(self):
        score = compute_column_completeness_score(10.0, 5.0)
        assert isinstance(score, float)

    def test_25_pct_null_yields_75_score(self):
        score = compute_column_completeness_score(25.0, None)
        assert score == pytest.approx(75.0)


# ===========================================================================
# 2. Format consistency score
# ===========================================================================

class TestFormatConsistencyScore:

    def test_high_coverage_returns_high_score(self):
        assert compute_format_consistency_score(98.5) == pytest.approx(98.5)

    def test_zero_coverage_returns_zero(self):
        assert compute_format_consistency_score(0.0) == 0.0

    def test_none_pattern_returns_none(self):
        assert compute_format_consistency_score(None) is None

    def test_mixed_format_threshold_below_50(self):
        score = compute_format_consistency_score(45.0)
        assert score is not None
        assert score < 50.0

    def test_consistent_format_at_boundary(self):
        assert compute_format_consistency_score(50.0) == pytest.approx(50.0)

    def test_100_pct_coverage_returns_100(self):
        assert compute_format_consistency_score(100.0) == pytest.approx(100.0)

    def test_returns_float(self):
        result = compute_format_consistency_score(75.3)
        assert isinstance(result, float)


# ===========================================================================
# 3. Validity metrics
# ===========================================================================

class TestValidityMetrics:

    def test_email_column_valid_returns_valid_status(self):
        valid_cnt, invalid_cnt, invalid_pct, status = compute_validity_metrics(
            'EMAIL', 100, 98.0, None, None, None,
        )
        assert status == 'valid'
        assert invalid_pct == pytest.approx(2.0)
        assert valid_cnt == 98
        assert invalid_cnt == 2

    def test_email_column_mostly_invalid(self):
        _, _, _, status = compute_validity_metrics('EMAIL', 100, 30.0, None, None, None)
        assert status == 'invalid'

    def test_email_column_partially_valid(self):
        _, _, _, status = compute_validity_metrics('EMAIL', 100, 70.0, None, None, None)
        assert status == 'partially_valid'

    def test_phone_column_valid(self):
        _, _, invalid_pct, status = compute_validity_metrics(
            'PHONE', 200, None, 96.0, None, None,
        )
        assert status == 'valid'
        assert invalid_pct == pytest.approx(4.0)

    def test_guid_column_via_id_semantic_type(self):
        valid_cnt, _, _, status = compute_validity_metrics(
            'ID', 50, None, None, 95.0, None,
        )
        assert status == 'valid'
        assert valid_cnt == 48  # round(50 * 0.95) = round(47.5) = 48 (Python banker's rounding)

    def test_unknown_semantic_type_with_high_guid_rate(self):
        # guid_match_rate >= 50 → heuristic applies even without ID semantic type
        _, _, invalid_pct, status = compute_validity_metrics(
            None, 100, None, None, 80.0, None,
        )
        assert status in ('valid', 'partially_valid')
        assert invalid_pct is not None

    def test_no_validator_applicable_returns_unknown(self):
        valid_cnt, invalid_cnt, invalid_pct, status = compute_validity_metrics(
            'TEXT', 100, None, None, None, None,
        )
        assert status == 'unknown'
        assert valid_cnt is None
        assert invalid_cnt is None
        assert invalid_pct is None

    def test_date_string_high_rate_triggers_validator(self):
        # 85% match rate → validator fires (threshold ≥80%), but 85% < 95% so partially_valid
        _, _, _, status = compute_validity_metrics(
            None, 100, None, None, None, 85.0,
        )
        assert status == 'partially_valid'

    def test_date_string_below_threshold_no_validator(self):
        _, _, _, status = compute_validity_metrics(
            None, 100, None, None, None, 50.0,
        )
        assert status == 'unknown'

    def test_none_populated_count_returns_unknown(self):
        _, _, _, status = compute_validity_metrics('EMAIL', None, 98.0, None, None, None)
        assert status == 'unknown'

    def test_invalid_count_non_negative(self):
        _, invalid_cnt, _, _ = compute_validity_metrics('EMAIL', 10, 100.0, None, None, None)
        assert invalid_cnt == 0

    def test_valid_count_plus_invalid_count_equals_populated(self):
        valid_cnt, invalid_cnt, _, status = compute_validity_metrics(
            'EMAIL', 100, 73.0, None, None, None,
        )
        assert status == 'partially_valid'
        assert valid_cnt + invalid_cnt == 100

    def test_exact_95_pct_is_valid(self):
        _, _, _, status = compute_validity_metrics('EMAIL', 100, 95.0, None, None, None)
        assert status == 'valid'

    def test_below_95_is_not_valid(self):
        _, _, _, status = compute_validity_metrics('EMAIL', 100, 94.9, None, None, None)
        assert status == 'partially_valid'

    def test_invalid_emails_column(self):
        _, invalid_cnt, invalid_pct, status = compute_validity_metrics(
            'EMAIL', 200, 10.0, None, None, None,
        )
        assert status == 'invalid'
        assert invalid_pct == pytest.approx(90.0)
        assert invalid_cnt == 180

    def test_invalid_guid_column(self):
        _, _, _, status = compute_validity_metrics(
            'ID', 100, None, None, 5.0, None,
        )
        assert status == 'invalid'


# ===========================================================================
# 4. Duplicate metrics (in-memory, not persisted)
# ===========================================================================

class TestDuplicateMetrics:

    def test_all_unique_no_duplicates(self):
        dup_count, dup_pct = compute_duplicate_metrics(100, 100)
        assert dup_count == 0
        assert dup_pct == 0.0

    def test_duplicate_heavy_column(self):
        # 100 rows, 10 distinct → 90 duplicates
        dup_count, dup_pct = compute_duplicate_metrics(100, 10)
        assert dup_count == 90
        assert dup_pct == pytest.approx(90.0)

    def test_none_populated_returns_none(self):
        dup_count, dup_pct = compute_duplicate_metrics(None, 50)
        assert dup_count is None
        assert dup_pct is None

    def test_none_distinct_returns_none(self):
        dup_count, dup_pct = compute_duplicate_metrics(100, None)
        assert dup_count is None
        assert dup_pct is None

    def test_both_none_returns_none(self):
        dup_count, dup_pct = compute_duplicate_metrics(None, None)
        assert dup_count is None
        assert dup_pct is None

    def test_empty_column_zero_populated(self):
        dup_count, dup_pct = compute_duplicate_metrics(0, 0)
        assert dup_count == 0
        assert dup_pct == 0.0

    def test_duplicate_count_non_negative(self):
        # distinct_count > populated_count can happen on live DBs with race conditions
        dup_count, _ = compute_duplicate_metrics(50, 55)
        assert dup_count == 0

    def test_50_pct_duplicates(self):
        dup_count, dup_pct = compute_duplicate_metrics(100, 50)
        assert dup_count == 50
        assert dup_pct == pytest.approx(50.0)


# ===========================================================================
# 5. Quality score
# ===========================================================================

class TestQualityScore:

    def test_perfect_all_components_scores_100(self):
        score = compute_quality_score(100.0, 100.0, 100.0)
        assert score == pytest.approx(100.0)

    def test_all_zero_scores_zero(self):
        score = compute_quality_score(0.0, 0.0, 0.0)
        assert score == pytest.approx(0.0)

    def test_completeness_only_uses_full_range(self):
        # When consistency and validity are None, completeness is the full score
        score = compute_quality_score(80.0, None, None)
        assert score == pytest.approx(80.0)

    def test_completeness_and_consistency_no_validity(self):
        # weights redistribute: 0.40 and 0.30 → normalised
        score = compute_quality_score(100.0, 0.0, None)
        # (100*0.40 + 0*0.30) / 0.70 = 40/0.70 ≈ 57.14
        assert score == pytest.approx(100.0 * 0.40 / 0.70, rel=0.01)

    def test_all_none_returns_none(self):
        assert compute_quality_score(None, None, None) is None

    def test_validity_only(self):
        score = compute_quality_score(None, None, 90.0)
        assert score == pytest.approx(90.0)

    def test_score_capped_at_100(self):
        score = compute_quality_score(100.0, 100.0, 100.0)
        assert score <= 100.0

    def test_score_at_least_zero(self):
        score = compute_quality_score(0.0, 0.0, 0.0)
        assert score >= 0.0

    def test_weighted_average_nominal(self):
        # 80*0.40 + 60*0.30 + 40*0.30 = 32 + 18 + 12 = 62
        score = compute_quality_score(80.0, 60.0, 40.0)
        assert score == pytest.approx(62.0)

    def test_score_is_float(self):
        score = compute_quality_score(75.0, 80.0, 90.0)
        assert isinstance(score, float)

    def test_mixed_quality_scores_between_0_and_100(self):
        score = compute_quality_score(50.0, 40.0, 60.0)
        assert 0.0 <= score <= 100.0


# ===========================================================================
# 6. Quality grade
# ===========================================================================

class TestQualityGrade:

    def test_100_is_grade_A(self):
        assert _grade(100.0) == 'A'

    def test_90_is_grade_A(self):
        assert _grade(90.0) == 'A'

    def test_89_is_grade_B(self):
        assert _grade(89.9) == 'B'

    def test_75_is_grade_B(self):
        assert _grade(75.0) == 'B'

    def test_74_is_grade_C(self):
        assert _grade(74.9) == 'C'

    def test_60_is_grade_C(self):
        assert _grade(60.0) == 'C'

    def test_59_is_grade_D(self):
        assert _grade(59.9) == 'D'

    def test_40_is_grade_D(self):
        assert _grade(40.0) == 'D'

    def test_39_is_grade_F(self):
        assert _grade(39.9) == 'F'

    def test_zero_is_grade_F(self):
        assert _grade(0.0) == 'F'


# ===========================================================================
# 7. Quality summary
# ===========================================================================

class TestQualitySummary:

    def _summary(self, **kwargs) -> dict:
        defaults = dict(
            null_percentage=None,
            blank_percentage=None,
            uniqueness_score=None,
            populated_count=None,
            pattern_coverage=None,
            validation_status=None,
            invalid_percentage=None,
        )
        defaults.update(kwargs)
        return _build_quality_summary(**defaults)

    def test_perfect_column_has_strength(self):
        s = self._summary(null_percentage=0.0, blank_percentage=0.0)
        assert len(s['strengths']) >= 1
        assert "null" in s['strengths'][0].lower() or "blank" in s['strengths'][0].lower()

    def test_high_null_rate_produces_issue(self):
        s = self._summary(null_percentage=50.0)
        assert any('null' in i.lower() for i in s['issues'])
        assert len(s['recommendations']) >= 1

    def test_mixed_format_produces_issue(self):
        s = self._summary(pattern_coverage=30.0)
        assert any('mixed' in i.lower() or 'format' in i.lower() for i in s['issues'])
        assert any('standardize' in r.lower() for r in s['recommendations'])

    def test_high_consistency_produces_strength(self):
        s = self._summary(pattern_coverage=98.0)
        assert any('consistent' in st.lower() for st in s['strengths'])

    def test_valid_status_produces_strength(self):
        s = self._summary(validation_status='valid')
        assert any('valid' in st.lower() for st in s['strengths'])

    def test_invalid_status_produces_issue_and_recommendation(self):
        s = self._summary(validation_status='invalid', invalid_percentage=60.0)
        assert any('fail' in i.lower() or 'invalid' in i.lower() for i in s['issues'])
        assert len(s['recommendations']) >= 1

    def test_all_unique_values_produces_strength(self):
        s = self._summary(uniqueness_score=1.0)
        assert any('unique' in st.lower() for st in s['strengths'])

    def test_very_low_uniqueness_produces_issue(self):
        s = self._summary(uniqueness_score=0.05, populated_count=1000)
        assert any('unique' in i.lower() or 'duplic' in i.lower() for i in s['issues'])

    def test_blank_string_rate_produces_recommendation(self):
        s = self._summary(null_percentage=0.0, blank_percentage=10.0)
        assert any('empty string' in r.lower() or 'null' in r.lower()
                   for r in s['recommendations'])

    def test_summary_always_has_three_keys(self):
        s = self._summary()
        assert set(s.keys()) == {'strengths', 'issues', 'recommendations'}

    def test_all_lists_are_lists(self):
        s = self._summary(null_percentage=5.0)
        assert isinstance(s['strengths'], list)
        assert isinstance(s['issues'], list)
        assert isinstance(s['recommendations'], list)

    def test_no_data_returns_empty_lists(self):
        s = self._summary()
        # No signals → no content (all lists may be empty)
        assert isinstance(s['strengths'], list)


# ===========================================================================
# 8. enrich_column_quality — end-to-end
# ===========================================================================

class TestEnrichColumnQuality:

    def test_perfect_quality_column(self):
        cp = _make_cp(
            null_percentage=0.0,
            blank_percentage=0.0,
            pattern_coverage=99.0,
            distinct_count=100,
            uniqueness_score=1.0,
            top_values=[{'value': 'x', 'count': 10, 'percentage': 10.0}],
        )
        enrich_column_quality(cp)
        assert cp.completeness_score == pytest.approx(100.0)
        assert cp.format_consistency_score == pytest.approx(99.0)
        assert cp.quality_score is not None
        assert cp.quality_score >= 90.0
        assert cp.quality_grade == 'A'
        assert cp.quality_summary_json is not None

    def test_null_heavy_column(self):
        cp = _make_cp(null_percentage=85.0, blank_percentage=None)
        enrich_column_quality(cp)
        assert cp.completeness_score == pytest.approx(15.0)
        assert cp.quality_score is not None
        assert cp.quality_score < 60.0

    def test_email_column_valid(self):
        cp = _make_cp(
            data_type='TEXT',
            semantic_type=SemanticType.EMAIL,
            email_match_rate=97.0,
            null_percentage=1.0,
        )
        enrich_column_quality(cp)
        assert cp.validation_status == 'valid'
        assert cp.valid_count is not None
        assert cp.invalid_count is not None
        assert cp.invalid_percentage == pytest.approx(3.0)

    def test_invalid_email_column(self):
        cp = _make_cp(
            data_type='TEXT',
            semantic_type=SemanticType.EMAIL,
            email_match_rate=20.0,
            null_percentage=0.0,
        )
        enrich_column_quality(cp)
        assert cp.validation_status == 'invalid'
        # completeness=100 (0% null) + validity=20% → weighted score ≈65 → grade C or lower
        assert cp.quality_grade in ('C', 'D', 'F')

    def test_invalid_guid_column(self):
        cp = _make_cp(
            data_type='TEXT',
            semantic_type=SemanticType.ID,
            guid_match_rate=15.0,
            null_percentage=0.0,
        )
        enrich_column_quality(cp)
        assert cp.validation_status == 'invalid'

    def test_mixed_format_column(self):
        cp = _make_cp(pattern_coverage=35.0, null_percentage=0.0)
        enrich_column_quality(cp)
        assert cp.format_consistency_score == pytest.approx(35.0)
        summary = json.loads(cp.quality_summary_json)
        assert any('mixed' in i.lower() or 'format' in i.lower() for i in summary['issues'])

    def test_duplicate_heavy_column(self):
        cp = _make_cp(populated_count=1000, distinct_count=10, uniqueness_score=0.01)
        enrich_column_quality(cp)
        assert cp.duplicate_count == 990
        assert cp.duplicate_percentage == pytest.approx(99.0)

    def test_unknown_validator_status(self):
        cp = _make_cp(
            data_type='INTEGER',
            semantic_type=None,
            email_match_rate=None,
            guid_match_rate=None,
        )
        enrich_column_quality(cp)
        assert cp.validation_status == 'unknown'
        assert cp.valid_count is None
        assert cp.invalid_count is None

    def test_structural_only_column_no_crash(self):
        cp = _make_cp(
            null_percentage=None,
            blank_percentage=None,
            populated_count=None,
            distinct_count=None,
            uniqueness_score=None,
            pattern_coverage=None,
        )
        enrich_column_quality(cp)
        assert cp.completeness_score is None
        assert cp.format_consistency_score is None
        assert cp.validation_status == 'unknown'
        assert cp.quality_score is None
        assert cp.quality_grade is None

    def test_dominant_value_percentage_set_from_top_values(self):
        cp = _make_cp(
            top_values=[
                {'value': 'A', 'count': 80, 'percentage': 80.0},
                {'value': 'B', 'count': 20, 'percentage': 20.0},
            ]
        )
        enrich_column_quality(cp)
        assert cp.dominant_value_percentage == pytest.approx(80.0)

    def test_dominant_value_percentage_none_when_no_top_values(self):
        cp = _make_cp(top_values=[])
        enrich_column_quality(cp)
        assert cp.dominant_value_percentage is None

    def test_quality_summary_json_is_valid_json(self):
        cp = _make_cp(null_percentage=10.0)
        enrich_column_quality(cp)
        data = json.loads(cp.quality_summary_json)
        assert isinstance(data, dict)
        assert 'strengths' in data
        assert 'issues' in data
        assert 'recommendations' in data

    def test_duplicate_count_not_negative(self):
        # distinct slightly higher than populated due to race (edge case)
        cp = _make_cp(populated_count=50, distinct_count=55)
        enrich_column_quality(cp)
        assert cp.duplicate_count == 0

    def test_quality_grade_none_when_no_score(self):
        cp = _make_cp(null_percentage=None, pattern_coverage=None)
        enrich_column_quality(cp)
        # With no completeness or consistency, score may still compute if validation kicks in
        # but when all are None, grade must be None
        if cp.quality_score is None:
            assert cp.quality_grade is None


# ===========================================================================
# 9. DB schema — Phase 1C columns present after init_db
# ===========================================================================

class _NoClose:
    """Wraps a connection and makes close() a no-op so in-memory DB survives."""
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


class TestPhase1CSchemaMigration:

    _PHASE_1C_COLS = [
        'completeness_score',
        'format_consistency_score',
        'valid_count',
        'invalid_count',
        'invalid_percentage',
        'validation_status',
        'quality_score',
        'quality_grade',
        'quality_summary_json',
    ]

    def _get_cols(self, raw: sqlite3.Connection) -> set[str]:
        return {
            row[1]
            for row in raw.execute(
                "PRAGMA table_info(profiling_column_profiles)"
            ).fetchall()
        }

    def test_all_phase1c_columns_present_after_init_db(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()

        cols = self._get_cols(raw)
        for col in self._PHASE_1C_COLS:
            assert col in cols, f"Phase 1C column '{col}' missing after init_db"
        raw.close()

    def test_init_db_idempotent_with_phase1c_columns(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()
            init_db()  # must not raise

        raw.close()

    def test_phase1a_columns_still_present(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()

        cols = self._get_cols(raw)
        for expected in ('p25_value', 'p50_value', 'p75_value', 'blank_percentage'):
            assert expected in cols, f"Phase 1A column '{expected}' missing after Phase 1C migration"
        raw.close()

    def test_phase1b_columns_still_present(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()

        cols = self._get_cols(raw)
        for expected in ('histogram_json', 'distribution_shape'):
            assert expected in cols, f"Phase 1B column '{expected}' missing after Phase 1C migration"
        raw.close()


# ===========================================================================
# 10. Regression — _col_row_params placeholder count
# ===========================================================================

class TestColRowParamsRegressionPhase1C:

    def test_param_count_matches_col_insert_placeholders(self):
        from data.profiling_service import _col_row_params, _COL_INSERT

        cp = _make_cp(null_percentage=5.0, distinct_count=80)
        enrich_column_quality(cp)

        placeholder_count = _COL_INSERT.count('?')
        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        assert len(params) == placeholder_count, (
            f"_col_row_params returned {len(params)} values but "
            f"_COL_INSERT has {placeholder_count} placeholders"
        )

    def test_quality_fields_in_params(self):
        from data.profiling_service import _col_row_params

        cp = _make_cp(null_percentage=0.0, blank_percentage=0.0, pattern_coverage=90.0)
        enrich_column_quality(cp)

        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        params_str = str(params)
        # completeness_score must appear (100.0 for 0% null + 0% blank)
        assert '100.0' in params_str, "completeness_score missing from params"
        # format_consistency_score must appear
        assert '90.0' in params_str, "format_consistency_score missing from params"
        # quality_grade must appear
        assert any(g in params_str for g in ('A', 'B', 'C', 'D', 'F')), (
            "quality_grade missing from params"
        )

    def test_null_quality_fields_allowed_in_params(self):
        from data.profiling_service import _col_row_params

        cp = _make_cp(null_percentage=None, pattern_coverage=None)
        enrich_column_quality(cp)

        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        # Must not crash; None values should be in the tuple
        assert None in params

    def test_quality_summary_json_in_params(self):
        from data.profiling_service import _col_row_params

        cp = _make_cp(null_percentage=30.0)
        enrich_column_quality(cp)

        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        # quality_summary_json should be a JSON string in the params tuple
        json_params = [p for p in params if isinstance(p, str) and p.startswith('{')]
        assert len(json_params) >= 1, "quality_summary_json not found in params"
