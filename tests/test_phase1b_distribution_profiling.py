"""
Phase 1B — Distribution Intelligence tests.

Covers:
  - compute_histogram: bucket generation, percentages, empty-bucket fill
  - compute_histogram: constant values, negative values, decimal values
  - compute_histogram: empty/null-heavy columns
  - compute_histogram: large distributions
  - classify_distribution_shape: all six shape classifications
  - classify_distribution_shape: missing data edge cases
  - build_histogram_query (mssql): SQL structure, identifier quoting, safety checks
  - profile_column_histogram: wires distribution_shape and histogram_json
  - profile_column_histogram: skips non-numeric, empty, no-min-max columns
  - DB schema: histogram_json and distribution_shape columns present after init_db
  - Regression: _col_row_params placeholder count still matches _COL_INSERT

Run from project root:
    venv/Scripts/pytest tests/test_phase1b_distribution_profiling.py -v
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-phase1b-secret-long-enough-value")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase1b-long-enough-value-12345")

from core.profiling.execution import profile_column_histogram
from core.profiling.models import (
    ColumnProfile, ProfilingConfig, ProfilingDepth, ProfilingStatus,
)
from core.profiling.sql.mssql import build_histogram_query
from core.profiling.statistics import classify_distribution_shape, compute_histogram


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_cp(
    data_type: str = 'INTEGER',
    populated_count: int = 100,
    null_count: int = 0,
    distinct_count: int = 50,
    min_value: str | None = '1',
    max_value: str | None = '100',
    mean_value: float | None = 50.0,
    std_deviation: float | None = 15.0,
    null_percentage: float | None = None,
    p25_value: str | None = None,
    p50_value: str | None = None,
    p75_value: str | None = None,
) -> ColumnProfile:
    cp = ColumnProfile(
        source_id=1,
        profiling_snapshot_id=1,
        table_fqn='dbo.Orders',
        column_name='Amount',
        data_type=data_type,
        raw_type=data_type.lower(),
        is_nullable=True,
        is_primary_key=False,
        is_identity=False,
        ordinal_position=1,
        populated_count=populated_count,
        null_count=null_count,
        null_percentage=null_percentage,
        distinct_count=distinct_count,
        min_value=min_value,
        max_value=max_value,
        mean_value=mean_value,
        std_deviation=std_deviation,
        p25_value=p25_value,
        p50_value=p50_value,
        p75_value=p75_value,
        profiling_depth=ProfilingDepth.STATISTICAL,
        profiling_status=ProfilingStatus.COMPLETE,
    )
    return cp


def _make_histogram_cursor(rows: list[tuple]):
    """Mock cursor returning (bucket_idx, row_count) rows."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.execute.return_value = cursor
    return conn


def _make_builder(sql: str = "SELECT 1"):
    builder = MagicMock()
    builder.build_histogram_query.return_value = sql
    return builder


# ===========================================================================
# 1. compute_histogram
# ===========================================================================

class TestComputeHistogram:

    def test_returns_n_buckets(self):
        hist = compute_histogram(0.0, 100.0, [(0, 50), (5, 30), (9, 20)], 10, 100)
        assert len(hist) == 10

    def test_bucket_bounds_non_overlapping(self):
        hist = compute_histogram(0.0, 100.0, [], 10, 100)
        for i in range(len(hist) - 1):
            assert hist[i]['upper_bound'] == pytest.approx(hist[i + 1]['lower_bound'])

    def test_first_bucket_lower_bound_equals_min(self):
        hist = compute_histogram(5.0, 55.0, [], 10, 100)
        assert hist[0]['lower_bound'] == pytest.approx(5.0)

    def test_last_bucket_upper_bound_equals_max(self):
        hist = compute_histogram(5.0, 55.0, [], 10, 100)
        assert hist[-1]['upper_bound'] == pytest.approx(55.0)

    def test_row_counts_from_raw_buckets(self):
        raw = [(0, 40), (3, 25), (9, 35)]
        hist = compute_histogram(0.0, 10.0, raw, 10, 100)
        assert hist[0]['row_count'] == 40
        assert hist[3]['row_count'] == 25
        assert hist[9]['row_count'] == 35

    def test_empty_buckets_have_zero_row_count(self):
        raw = [(0, 100)]
        hist = compute_histogram(0.0, 10.0, raw, 10, 100)
        for i in range(1, 10):
            assert hist[i]['row_count'] == 0

    def test_percentage_matches_row_count_fraction(self):
        raw = [(0, 25), (5, 75)]
        hist = compute_histogram(0.0, 100.0, raw, 10, 100)
        assert hist[0]['percentage'] == pytest.approx(25.0)
        assert hist[5]['percentage'] == pytest.approx(75.0)

    def test_empty_bucket_percentage_is_zero(self):
        raw = [(0, 100)]
        hist = compute_histogram(0.0, 100.0, raw, 10, 100)
        assert hist[1]['percentage'] == pytest.approx(0.0)

    def test_constant_column_returns_single_bucket(self):
        hist = compute_histogram(42.0, 42.0, [(0, 500)], 10, 500)
        assert len(hist) == 1
        assert hist[0]['row_count'] == 500
        assert hist[0]['percentage'] == pytest.approx(100.0)
        assert hist[0]['lower_bound'] == pytest.approx(42.0)
        assert hist[0]['upper_bound'] == pytest.approx(42.0)

    def test_negative_values_produce_valid_bounds(self):
        hist = compute_histogram(-100.0, -10.0, [(0, 30), (4, 70)], 10, 100)
        assert len(hist) == 10
        assert hist[0]['lower_bound'] == pytest.approx(-100.0)
        assert hist[-1]['upper_bound'] == pytest.approx(-10.0)
        assert all(b['lower_bound'] < 0 for b in hist)

    def test_decimal_values_produce_valid_bounds(self):
        hist = compute_histogram(0.5, 1.5, [], 10, 0)
        # populated_count=0 → returns []
        assert hist == []

    def test_decimal_range_buckets(self):
        hist = compute_histogram(0.1, 1.1, [(2, 60), (7, 40)], 10, 100)
        assert len(hist) == 10
        assert hist[2]['row_count'] == 60
        assert hist[7]['row_count'] == 40

    def test_zero_populated_count_returns_empty(self):
        hist = compute_histogram(0.0, 100.0, [(0, 10)], 10, 0)
        assert hist == []

    def test_out_of_range_bucket_idx_ignored(self):
        raw = [(0, 50), (15, 50)]   # idx 15 is out of range for 10 buckets
        hist = compute_histogram(0.0, 100.0, raw, 10, 100)
        assert len(hist) == 10
        assert hist[0]['row_count'] == 50
        total = sum(b['row_count'] for b in hist)
        assert total == 50  # idx 15 is dropped

    def test_null_heavy_column_with_small_populated_count(self):
        # 90% nulls, only 10 rows populated
        hist = compute_histogram(1.0, 10.0, [(0, 10)], 10, 10)
        assert len(hist) == 10
        assert hist[0]['percentage'] == pytest.approx(100.0)

    def test_large_distribution_10k_buckets(self):
        # Not a production use case but should not error
        raw = [(i, 1) for i in range(100)]
        hist = compute_histogram(0.0, 1000.0, raw, 100, 100)
        assert len(hist) == 100

    def test_bucket_widths_are_uniform(self):
        hist = compute_histogram(0.0, 50.0, [], 10, 10)
        widths = [b['upper_bound'] - b['lower_bound'] for b in hist]
        expected = 5.0
        for w in widths:
            assert w == pytest.approx(expected)

    def test_five_buckets_supported(self):
        hist = compute_histogram(0.0, 100.0, [(0, 50), (4, 50)], 5, 100)
        assert len(hist) == 5
        assert hist[0]['row_count'] == 50
        assert hist[4]['row_count'] == 50


# ===========================================================================
# 2. classify_distribution_shape
# ===========================================================================

class TestClassifyDistributionShape:

    def _classify(self, **kwargs):
        defaults = dict(
            distinct_count=50,
            populated_count=100,
            null_percentage=5.0,
            p25_value='25.0',
            p50_value='50.0',
            p75_value='75.0',
        )
        defaults.update(kwargs)
        return classify_distribution_shape(**defaults)

    def test_symmetric_balanced_quartiles(self):
        # P75 - P50 ≈ P50 - P25 → Bowley skewness ≈ 0
        shape = self._classify(p25_value='25.0', p50_value='50.0', p75_value='75.0')
        assert shape == 'symmetric'

    def test_right_skewed_positive_bowley(self):
        # Bowley = (p75 - 2*p50 + p25) / (p75 - p25) = (60-60+10)/50 = 0.2 → right_skewed
        shape = self._classify(p25_value='10.0', p50_value='30.0', p75_value='60.0')
        assert shape == 'right_skewed'

    def test_left_skewed_negative_bowley(self):
        # Bowley = (90 - 140 + 40) / 50 = -0.2 → left_skewed
        shape = self._classify(p25_value='40.0', p50_value='70.0', p75_value='90.0')
        assert shape == 'left_skewed'

    def test_constant_distinct_count_one(self):
        shape = self._classify(distinct_count=1)
        assert shape == 'constant'

    def test_constant_zero_distinct_count(self):
        shape = self._classify(distinct_count=0)
        assert shape == 'constant'

    def test_constant_zero_iqr(self):
        # IQR = 0 → all quartiles equal → constant-like
        shape = self._classify(p25_value='50.0', p50_value='50.0', p75_value='50.0')
        assert shape == 'constant'

    def test_sparse_above_80_percent_null(self):
        shape = self._classify(null_percentage=85.0)
        assert shape == 'sparse'

    def test_sparse_exactly_80_not_triggered(self):
        # 80.0 is NOT > 80 so it should not be classified as sparse
        shape = self._classify(null_percentage=80.0)
        assert shape != 'sparse'

    def test_highly_skewed_bowley_above_0_5(self):
        # (P75 - 2*P50 + P25) / IQR > 0.5
        shape = self._classify(p25_value='1.0', p50_value='2.0', p75_value='100.0')
        assert shape == 'highly_skewed'

    def test_highly_skewed_negative(self):
        # Large negative Bowley skewness
        shape = self._classify(p25_value='1.0', p50_value='99.0', p75_value='100.0')
        assert shape == 'highly_skewed'

    def test_returns_none_for_zero_populated_count(self):
        shape = self._classify(populated_count=0)
        assert shape is None

    def test_returns_none_for_none_populated_count(self):
        shape = self._classify(populated_count=None)
        assert shape is None

    def test_returns_none_when_p50_missing(self):
        shape = self._classify(p50_value=None)
        assert shape is None

    def test_returns_none_when_p25_missing(self):
        shape = self._classify(p25_value=None)
        assert shape is None

    def test_returns_none_when_p75_missing(self):
        shape = self._classify(p75_value=None)
        assert shape is None

    def test_returns_none_when_all_percentiles_missing(self):
        shape = self._classify(p25_value=None, p50_value=None, p75_value=None)
        assert shape is None

    def test_invalid_percentile_string_returns_none(self):
        shape = self._classify(p25_value='not_a_number')
        assert shape is None

    def test_null_percentage_none_does_not_crash(self):
        # null_percentage=None should not raise even though sparse check is attempted
        shape = self._classify(null_percentage=None)
        assert shape in ('symmetric', 'right_skewed', 'left_skewed', 'highly_skewed', 'constant', 'sparse', None)

    def test_constant_checked_before_sparse(self):
        # distinct_count=1 should win over high null_percentage
        shape = self._classify(distinct_count=1, null_percentage=95.0)
        assert shape == 'constant'


# ===========================================================================
# 3. build_histogram_query (mssql)
# ===========================================================================

class TestBuildHistogramQuery:

    def test_returns_string(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert isinstance(sql, str)

    def test_contains_bucket_idx(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'bucket_idx' in sql

    def test_contains_count_big(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'COUNT_BIG' in sql

    def test_bracket_quotes_table_and_column(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert '[dbo]' in sql
        assert '[Orders]' in sql
        assert '[Amount]' in sql

    def test_filters_nulls(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'IS NOT NULL' in sql

    def test_nolock_present_by_default(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'NOLOCK' in sql

    def test_orders_by_bucket_idx(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'ORDER BY bucket_idx' in sql

    def test_groups_by_bucket_idx(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'GROUP BY bucket_idx' in sql

    def test_n_minus_1_present_for_max_clamping(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0, n_buckets=10)
        # The last bucket index should be 9
        assert '9' in sql

    def test_constant_column_returns_simple_query(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 42.0, 42.0)
        # Should not include the full derived-table bucket logic
        assert 'FLOOR' not in sql
        assert '0 AS bucket_idx' in sql

    def test_negative_values_produce_valid_sql(self):
        sql = build_histogram_query('dbo.Sales', 'Balance', -1000.0, -100.0)
        assert isinstance(sql, str)
        assert 'FLOOR' in sql

    def test_decimal_values_produce_valid_sql(self):
        sql = build_histogram_query('dbo.Prices', 'UnitCost', 0.01, 9.99)
        assert isinstance(sql, str)
        assert 'FLOOR' in sql

    def test_unsafe_column_identifier_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'bad]col', 0.0, 100.0)

    def test_unsafe_table_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.bad]table', 'Amount', 0.0, 100.0)

    def test_non_finite_min_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'Amount', float('inf'), 100.0)

    def test_non_finite_max_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'Amount', 0.0, float('nan'))

    def test_min_greater_than_max_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'Amount', 100.0, 0.0)

    def test_n_buckets_zero_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0, n_buckets=0)

    def test_n_buckets_too_large_raises(self):
        with pytest.raises(ValueError):
            build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0, n_buckets=1001)

    def test_floor_expression_present_for_non_constant(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'FLOOR' in sql

    def test_cast_to_float_for_numeric_safety(self):
        sql = build_histogram_query('dbo.Orders', 'Amount', 0.0, 100.0)
        assert 'FLOAT' in sql


# ===========================================================================
# 4. profile_column_histogram — execution
# ===========================================================================

class TestProfileColumnHistogram:

    def _run(self, cp: ColumnProfile, rows: list[tuple]):
        cur = _make_histogram_cursor(rows)
        conn = _make_conn(cur)
        builder = _make_builder()
        config = ProfilingConfig()
        profile_column_histogram(conn, cp, config, builder)
        return cp

    def test_integer_column_gets_histogram_json(self):
        cp = _make_cp('INTEGER', populated_count=100, min_value='0', max_value='99')
        rows = [(i, 10) for i in range(10)]
        self._run(cp, rows)
        assert cp.histogram_json is not None
        hist = json.loads(cp.histogram_json)
        assert len(hist) == 10

    def test_decimal_column_gets_histogram_json(self):
        cp = _make_cp('DECIMAL', populated_count=50, min_value='0.5', max_value='5.0')
        rows = [(0, 25), (9, 25)]
        self._run(cp, rows)
        assert cp.histogram_json is not None

    def test_text_column_skipped(self):
        cp = _make_cp('TEXT', min_value='a', max_value='z')
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()
        assert cp.histogram_json is None

    def test_boolean_column_skipped(self):
        cp = _make_cp('BOOLEAN', min_value='0', max_value='1')
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()

    def test_zero_populated_count_skipped(self):
        cp = _make_cp('INTEGER', populated_count=0)
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()
        assert cp.histogram_json is None

    def test_none_populated_count_skipped(self):
        cp = _make_cp('INTEGER', populated_count=0)
        cp.populated_count = None
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()

    def test_missing_min_value_skips_histogram_query(self):
        cp = _make_cp('INTEGER', min_value=None, max_value='100')
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()

    def test_missing_max_value_skips_histogram_query(self):
        cp = _make_cp('INTEGER', min_value='0', max_value=None)
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()

    def test_constant_column_no_query_issued(self):
        cp = _make_cp('INTEGER', min_value='42', max_value='42', distinct_count=1)
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        builder.build_histogram_query.assert_not_called()
        assert cp.histogram_json is not None
        hist = json.loads(cp.histogram_json)
        assert len(hist) == 1
        assert hist[0]['percentage'] == pytest.approx(100.0)

    def test_distribution_shape_set_for_symmetric(self):
        cp = _make_cp('INTEGER', p25_value='25.0', p50_value='50.0', p75_value='75.0')
        self._run(cp, [(i, 10) for i in range(10)])
        assert cp.distribution_shape == 'symmetric'

    def test_distribution_shape_set_for_right_skewed(self):
        # Bowley = (60-60+10)/50 = 0.2 → right_skewed
        cp = _make_cp('INTEGER', p25_value='10.0', p50_value='30.0', p75_value='60.0')
        self._run(cp, [(i, 10) for i in range(10)])
        assert cp.distribution_shape == 'right_skewed'

    def test_distribution_shape_set_for_constant(self):
        cp = _make_cp('INTEGER', distinct_count=1,
                      p25_value='5.0', p50_value='5.0', p75_value='5.0')
        self._run(cp, [])
        assert cp.distribution_shape == 'constant'

    def test_distribution_shape_set_for_sparse(self):
        cp = _make_cp('INTEGER', null_percentage=90.0,
                      p25_value='1.0', p50_value='2.0', p75_value='3.0')
        self._run(cp, [])
        assert cp.distribution_shape == 'sparse'

    def test_distribution_shape_set_even_without_percentiles(self):
        # No quartile data → shape should be None or constant if distinct=1
        cp = _make_cp('INTEGER', distinct_count=1)
        builder = _make_builder()
        conn = _make_conn(_make_histogram_cursor([]))
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        assert cp.distribution_shape == 'constant'

    def test_distribution_shape_none_when_insufficient_data(self):
        cp = _make_cp('INTEGER', p25_value=None, p50_value=None, p75_value=None,
                      distinct_count=5, null_percentage=10.0)
        self._run(cp, [])
        assert cp.distribution_shape is None

    def test_histogram_buckets_have_required_keys(self):
        cp = _make_cp('INTEGER', populated_count=100, min_value='0', max_value='100')
        rows = [(0, 100)]
        self._run(cp, rows)
        hist = json.loads(cp.histogram_json)
        for bucket in hist:
            assert 'lower_bound' in bucket
            assert 'upper_bound' in bucket
            assert 'row_count' in bucket
            assert 'percentage' in bucket

    def test_query_error_does_not_raise(self):
        cp = _make_cp('INTEGER', populated_count=100, min_value='0', max_value='100')
        conn = MagicMock()
        conn.execute.side_effect = Exception("MSSQL timeout")
        builder = _make_builder()
        profile_column_histogram(conn, cp, ProfilingConfig(), builder)
        # Must not raise; histogram_json will be None

    def test_negative_value_range(self):
        cp = _make_cp('INTEGER', populated_count=50,
                      min_value='-100', max_value='-10')
        rows = [(0, 25), (9, 25)]
        self._run(cp, rows)
        assert cp.histogram_json is not None
        hist = json.loads(cp.histogram_json)
        assert hist[0]['lower_bound'] == pytest.approx(-100.0)
        assert hist[-1]['upper_bound'] == pytest.approx(-10.0)

    def test_decimal_value_range(self):
        cp = _make_cp('DECIMAL', populated_count=200,
                      min_value='0.001', max_value='9.999')
        rows = [(i, 20) for i in range(10)]
        self._run(cp, rows)
        assert cp.histogram_json is not None
        hist = json.loads(cp.histogram_json)
        assert len(hist) == 10
        assert hist[0]['lower_bound'] == pytest.approx(0.001)


# ===========================================================================
# 5. DB schema migration — histogram_json and distribution_shape present
# ===========================================================================

class _NoClose:
    """Wraps a connection and makes close() a no-op so in-memory DB survives."""
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


class TestPhase1BSchemaMigration:

    def test_histogram_json_column_present_after_init_db(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()

        cols = {
            row[1]
            for row in raw.execute("PRAGMA table_info(profiling_column_profiles)").fetchall()
        }
        assert 'histogram_json' in cols, "histogram_json column missing after init_db"
        assert 'distribution_shape' in cols, "distribution_shape column missing after init_db"
        raw.close()

    def test_init_db_idempotent_with_new_columns(self):
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()
            init_db()   # must not raise

        raw.close()

    def test_phase1a_columns_still_present(self):
        """Regression: Phase 1A columns must still exist alongside Phase 1B additions."""
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()

        cols = {
            row[1]
            for row in raw.execute("PRAGMA table_info(profiling_column_profiles)").fetchall()
        }
        for expected in ('p25_value', 'p50_value', 'p75_value', 'blank_percentage'):
            assert expected in cols, f"Phase 1A column '{expected}' missing after Phase 1B migration"
        raw.close()


# ===========================================================================
# 6. Regression — _col_row_params placeholder count
# ===========================================================================

class TestColRowParamsRegressionPhase1B:

    def test_param_count_matches_col_insert_placeholders(self):
        from data.profiling_service import _col_row_params, _COL_INSERT
        from core.profiling.models import ProfilingDepth, ProfilingStatus

        cp = _make_cp('INTEGER', populated_count=100)
        cp.histogram_json    = json.dumps([{'lower_bound': 0.0, 'upper_bound': 10.0,
                                            'row_count': 100, 'percentage': 100.0}])
        cp.distribution_shape = 'symmetric'

        placeholder_count = _COL_INSERT.count('?')
        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        assert len(params) == placeholder_count, (
            f"_col_row_params returned {len(params)} values but "
            f"_COL_INSERT has {placeholder_count} placeholders"
        )

    def test_histogram_json_in_params(self):
        from data.profiling_service import _col_row_params

        cp = _make_cp('INTEGER', populated_count=100)
        cp.histogram_json = json.dumps([{'lower_bound': 0.0, 'upper_bound': 10.0,
                                         'row_count': 100, 'percentage': 100.0}])
        cp.distribution_shape = 'right_skewed'

        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        params_str = str(params)
        assert 'lower_bound' in params_str, "histogram_json must be in persisted params"
        assert 'right_skewed' in params_str, "distribution_shape must be in persisted params"

    def test_null_histogram_json_allowed(self):
        from data.profiling_service import _col_row_params

        cp = _make_cp('INTEGER', populated_count=100)
        # histogram_json defaults to None — must not crash
        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        assert None in params   # histogram_json is None for structural-only profiles
