"""
Phase 1A — Deep Statistical Profiling tests.

Covers:
  - build_percentile_query: correct MSSQL SQL output, identifier quoting
  - profile_column_percentiles: populates p5/p25/p50/p75/p95 for INTEGER/DECIMAL
  - profile_column_percentiles: skips TEXT, BOOLEAN, and zero-row columns safely
  - profile_column_statistics: blank_percentage derived for TEXT columns
  - profile_column_statistics: variance derived from std_deviation for numeric columns
  - profiling_service._col_row_params: new fields present in parameter tuple
  - data/models.py ALTER TABLE migrations: idempotent on repeated init

Run from project root:
    venv/Scripts/pytest tests/test_phase1a_statistical_profiling.py -v
"""
from __future__ import annotations

import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET",   "test-jwt-phase1a-secret-long-enough-value")
os.environ.setdefault("USER_ID_SALT", "test-salt-phase1a-long-enough-value-12345")

from core.profiling.execution import (
    profile_column_percentiles,
    profile_column_statistics,
)
from core.profiling.models import ColumnProfile, ProfilingConfig, ProfilingDepth, ProfilingStatus
from core.profiling.sql.mssql import build_percentile_query


# ---------------------------------------------------------------------------
# Minimal schema for service-layer tests
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE profiling_column_profiles (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        profiling_snapshot_id INTEGER NOT NULL,
        source_id             INTEGER NOT NULL,
        table_fqn             TEXT    NOT NULL,
        column_name           TEXT    NOT NULL,
        data_type             TEXT    NOT NULL DEFAULT 'TEXT',
        raw_type              TEXT,
        is_nullable           INTEGER NOT NULL DEFAULT 1,
        is_primary_key        INTEGER NOT NULL DEFAULT 0,
        is_identity           INTEGER NOT NULL DEFAULT 0,
        ordinal_position      INTEGER NOT NULL DEFAULT 0,
        null_count            INTEGER,
        null_percentage       REAL,
        populated_count       INTEGER,
        populated_percentage  REAL,
        empty_string_count    INTEGER,
        zero_count            INTEGER,
        distinct_count        INTEGER,
        distinct_percentage   REAL,
        uniqueness_score      REAL,
        cardinality_tier      TEXT,
        min_value             TEXT,
        max_value             TEXT,
        min_length            INTEGER,
        max_length_observed   INTEGER,
        avg_length            REAL,
        mean_value            REAL,
        std_deviation         REAL,
        p5_value              TEXT,
        p25_value             TEXT,
        p50_value             TEXT,
        p75_value             TEXT,
        p95_value             TEXT,
        blank_percentage      REAL,
        dominant_pattern      TEXT,
        pattern_coverage      REAL,
        email_match_rate      REAL,
        phone_match_rate      REAL,
        guid_match_rate       REAL,
        date_string_rate      REAL,
        numeric_string_rate   REAL,
        masked_value_rate     REAL,
        semantic_type         TEXT,
        semantic_confidence   REAL,
        semantic_evidence_json TEXT,
        semantic_rule_version TEXT,
        pii_name_heuristic    INTEGER NOT NULL DEFAULT 0,
        pii_confirmed         INTEGER NOT NULL DEFAULT 0,
        pii_signals_json      TEXT,
        top_values_coverage   REAL,
        profiling_depth       TEXT    NOT NULL DEFAULT 'STRUCTURAL_ONLY',
        profiling_duration_ms INTEGER,
        profiling_status      TEXT    NOT NULL DEFAULT 'COMPLETE',
        created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cp(data_type: str = 'INTEGER', populated_count: int = 100) -> ColumnProfile:
    return ColumnProfile(
        source_id=1,
        profiling_snapshot_id=1,
        table_fqn='dbo.Sales',
        column_name='Amount',
        data_type=data_type,
        raw_type=data_type.lower(),
        is_nullable=True,
        is_primary_key=False,
        is_identity=False,
        ordinal_position=1,
        populated_count=populated_count,
        null_count=0,
        profiling_depth=ProfilingDepth.STATISTICAL,
        profiling_status=ProfilingStatus.COMPLETE,
    )


def _make_cursor(row: dict | None):
    """Mock DBAPI2 cursor that returns one row from fetchone()."""
    cur = MagicMock()
    if row is None:
        cur.fetchone.return_value = None
        cur.description = []
    else:
        cur.description = [(k, None, None, None, None, None, None) for k in row]
        cur.fetchone.return_value = list(row.values())
    return cur


def _make_conn(cursor):
    conn = MagicMock()
    conn.execute.return_value = cursor
    return conn


def _make_builder(sql: str = "SELECT 1"):
    builder = MagicMock()
    builder.build_column_stats_query.return_value = sql
    builder.build_percentile_query.return_value = sql
    return builder


# ---------------------------------------------------------------------------
# 1. SQL generation — build_percentile_query
# ---------------------------------------------------------------------------

class TestBuildPercentileQuery:

    def test_returns_string(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert isinstance(sql, str)

    def test_contains_percentile_cont(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert 'PERCENTILE_CONT' in sql

    def test_all_five_percentiles_present(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        for alias in ('p5_value', 'p25_value', 'p50_value', 'p75_value', 'p95_value'):
            assert alias in sql, f"Expected alias '{alias}' in SQL"

    def test_bracket_quotes_table_and_column(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert '[dbo]' in sql
        assert '[Sales]' in sql
        assert '[Amount]' in sql

    def test_filters_nulls(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert 'IS NOT NULL' in sql

    def test_uses_top_1_for_single_row(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert 'TOP (1)' in sql

    def test_nolock_included_by_default(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert 'NOLOCK' in sql

    def test_unsafe_identifier_raises(self):
        with pytest.raises(ValueError):
            build_percentile_query('dbo.Sales', 'bad]col')

    def test_p5_fraction_value(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert '0.05' in sql

    def test_p95_fraction_value(self):
        sql = build_percentile_query('dbo.Sales', 'Amount')
        assert '0.95' in sql


# ---------------------------------------------------------------------------
# 2. profile_column_percentiles — numeric columns
# ---------------------------------------------------------------------------

class TestProfileColumnPercentiles:

    def _run(self, cp: ColumnProfile, row: dict | None):
        cur = _make_cursor(row)
        conn = _make_conn(cur)
        builder = _make_builder()
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        return cp

    def test_integer_column_gets_all_five_percentiles(self):
        cp = _make_cp('INTEGER', populated_count=50)
        row = {
            'p5_value': '2.0', 'p25_value': '10.0',
            'p50_value': '25.0', 'p75_value': '40.0', 'p95_value': '48.0',
        }
        self._run(cp, row)
        assert cp.p5_value  == '2.0'
        assert cp.p25_value == '10.0'
        assert cp.p50_value == '25.0'
        assert cp.p75_value == '40.0'
        assert cp.p95_value == '48.0'

    def test_decimal_column_gets_all_five_percentiles(self):
        cp = _make_cp('DECIMAL', populated_count=100)
        row = {
            'p5_value': '1.5', 'p25_value': '12.5',
            'p50_value': '50.0', 'p75_value': '87.5', 'p95_value': '98.5',
        }
        self._run(cp, row)
        assert cp.p50_value == '50.0'
        assert cp.p25_value == '12.5'
        assert cp.p75_value == '87.5'

    def test_median_populated_for_numeric(self):
        cp = _make_cp('INTEGER', populated_count=200)
        row = {
            'p5_value': '5.0', 'p25_value': '25.0',
            'p50_value': '50.0', 'p75_value': '75.0', 'p95_value': '95.0',
        }
        self._run(cp, row)
        assert cp.p50_value is not None, "median (p50_value) must be populated for numeric columns"

    def test_existing_p5_p95_populated(self):
        cp = _make_cp('INTEGER', populated_count=100)
        row = {
            'p5_value': '3.0', 'p25_value': '20.0',
            'p50_value': '45.0', 'p75_value': '70.0', 'p95_value': '92.0',
        }
        self._run(cp, row)
        assert cp.p5_value  == '3.0',  "p5_value must be populated"
        assert cp.p95_value == '92.0', "p95_value must be populated"

    def test_text_column_skipped_safely(self):
        cp = _make_cp('TEXT', populated_count=100)
        builder = _make_builder()
        conn = _make_conn(_make_cursor(None))
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        builder.build_percentile_query.assert_not_called()
        assert cp.p5_value is None
        assert cp.p50_value is None

    def test_boolean_column_skipped_safely(self):
        cp = _make_cp('BOOLEAN', populated_count=100)
        builder = _make_builder()
        conn = _make_conn(_make_cursor(None))
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        builder.build_percentile_query.assert_not_called()

    def test_datetime_column_skipped_safely(self):
        cp = _make_cp('DATETIME', populated_count=100)
        builder = _make_builder()
        conn = _make_conn(_make_cursor(None))
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        builder.build_percentile_query.assert_not_called()

    def test_zero_populated_count_skipped(self):
        cp = _make_cp('INTEGER', populated_count=0)
        builder = _make_builder()
        conn = _make_conn(_make_cursor(None))
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        builder.build_percentile_query.assert_not_called()

    def test_none_populated_count_skipped(self):
        cp = _make_cp('INTEGER', populated_count=0)
        cp.populated_count = None
        builder = _make_builder()
        conn = _make_conn(_make_cursor(None))
        config = ProfilingConfig()
        profile_column_percentiles(conn, cp, config, builder)
        builder.build_percentile_query.assert_not_called()

    def test_empty_cursor_result_leaves_values_none(self):
        cp = _make_cp('INTEGER', populated_count=50)
        self._run(cp, None)
        assert cp.p50_value is None

    def test_query_error_does_not_raise(self):
        cp = _make_cp('INTEGER', populated_count=50)
        conn = MagicMock()
        conn.execute.side_effect = Exception("MSSQL timeout")
        builder = _make_builder()
        profile_column_percentiles(conn, cp, ProfilingConfig(), builder)
        # must survive without raising


# ---------------------------------------------------------------------------
# 3. blank_percentage — derived from TEXT column stats
# ---------------------------------------------------------------------------

class TestBlankPercentage:

    def _run_col_stats(self, data_type: str, row: dict) -> ColumnProfile:
        cp = _make_cp(data_type, populated_count=None)
        cur = _make_cursor(row)
        conn = _make_conn(cur)
        builder = MagicMock()
        builder.build_column_stats_query.return_value = "SELECT 1"
        config = ProfilingConfig()
        profile_column_statistics(conn, cp, config, builder)
        return cp

    def test_blank_percentage_computed_for_text(self):
        row = {
            'total_rows': 100, 'populated_count': 90, 'null_count': 10,
            'distinct_count': 50, 'min_value': 'a', 'max_value': 'z',
            'min_length': 1, 'max_length_observed': 20, 'avg_length': 8.0,
            'empty_string_count': 5,
        }
        cp = self._run_col_stats('TEXT', row)
        assert cp.blank_percentage is not None
        assert abs(cp.blank_percentage - 5.0) < 0.001  # 5/100 * 100

    def test_blank_percentage_zero_when_no_empty_strings(self):
        row = {
            'total_rows': 200, 'populated_count': 200, 'null_count': 0,
            'distinct_count': 10, 'min_value': 'x', 'max_value': 'z',
            'min_length': 1, 'max_length_observed': 5, 'avg_length': 3.0,
            'empty_string_count': 0,
        }
        cp = self._run_col_stats('TEXT', row)
        assert cp.blank_percentage == 0.0

    def test_blank_percentage_not_set_for_integer(self):
        row = {
            'total_rows': 100, 'populated_count': 95, 'null_count': 5,
            'distinct_count': 30, 'min_value': '1', 'max_value': '99',
            'mean_value': 50.0, 'std_deviation': 15.0, 'zero_count': 2,
        }
        cp = self._run_col_stats('INTEGER', row)
        assert cp.blank_percentage is None


# ---------------------------------------------------------------------------
# 4. variance — derived from std_deviation (not stored)
# ---------------------------------------------------------------------------

class TestVarianceDerived:

    def _run_col_stats(self, row: dict) -> ColumnProfile:
        cp = _make_cp('INTEGER', populated_count=None)
        cur = _make_cursor(row)
        conn = _make_conn(cur)
        builder = MagicMock()
        builder.build_column_stats_query.return_value = "SELECT 1"
        profile_column_statistics(conn, cp, ProfilingConfig(), builder)
        return cp

    def test_variance_equals_std_deviation_squared(self):
        row = {
            'total_rows': 100, 'populated_count': 100, 'null_count': 0,
            'distinct_count': 50, 'min_value': '1', 'max_value': '100',
            'mean_value': 50.0, 'std_deviation': 4.0, 'zero_count': 0,
        }
        cp = self._run_col_stats(row)
        assert cp.std_deviation == 4.0
        assert cp.variance is not None
        assert abs(cp.variance - 16.0) < 0.001  # 4.0 ** 2

    def test_variance_none_when_std_deviation_none(self):
        row = {
            'total_rows': 100, 'populated_count': 100, 'null_count': 0,
            'distinct_count': 50, 'min_value': '1', 'max_value': '100',
            'mean_value': 50.0, 'std_deviation': None, 'zero_count': 0,
        }
        cp = self._run_col_stats(row)
        assert cp.variance is None

    def test_variance_not_set_for_text_column(self):
        cp = _make_cp('TEXT', populated_count=None)
        row = {
            'total_rows': 100, 'populated_count': 90, 'null_count': 10,
            'distinct_count': 30, 'min_value': 'a', 'max_value': 'z',
            'min_length': 1, 'max_length_observed': 10, 'avg_length': 5.0,
            'empty_string_count': 0,
        }
        cur = _make_cursor(row)
        conn = _make_conn(cur)
        builder = MagicMock()
        builder.build_column_stats_query.return_value = "SELECT 1"
        profile_column_statistics(conn, cp, ProfilingConfig(), builder)
        assert cp.variance is None


# ---------------------------------------------------------------------------
# 5. _col_row_params — new fields in persisted tuple
# ---------------------------------------------------------------------------

class TestColRowParamsNewFields:

    def test_new_percentile_and_blank_fields_present_in_params(self):
        from data.profiling_service import _col_row_params
        from core.profiling.models import (
            CardinalityTier, ProfilingDepth, ProfilingStatus, SemanticType,
        )

        cp = _make_cp('INTEGER', populated_count=100)
        cp.p5_value  = '1.0'
        cp.p25_value = '10.0'
        cp.p50_value = '50.0'
        cp.p75_value = '75.0'
        cp.p95_value = '99.0'
        cp.blank_percentage = None
        cp.variance  = 25.0   # in-memory only — must NOT appear in params tuple

        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        params_str = str(params)

        assert '1.0'  in params_str, "p5_value must be in persisted params"
        assert '10.0' in params_str, "p25_value must be in persisted params"
        assert '50.0' in params_str, "p50_value must be in persisted params"
        assert '75.0' in params_str, "p75_value must be in persisted params"
        assert '99.0' in params_str, "p95_value must be in persisted params"

    def test_param_count_matches_col_insert_placeholders(self):
        from data.profiling_service import _col_row_params, _COL_INSERT

        placeholder_count = _COL_INSERT.count('?')
        cp = _make_cp('INTEGER', populated_count=50)
        params = _col_row_params(cp, snap_id=1, rule_version='4.0.0', now='2026-07-02T00:00:00')
        assert len(params) == placeholder_count, (
            f"_col_row_params returned {len(params)} values but _COL_INSERT has {placeholder_count} placeholders"
        )


# ---------------------------------------------------------------------------
# 6. Idempotent schema migrations — data/models.py init_db
# ---------------------------------------------------------------------------

class _NoClose:
    """Wraps a connection and makes close() a no-op so in-memory DB survives."""
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


class TestSchemaIdempotentMigrations:

    def test_new_columns_present_after_init_db(self):
        """init_db must add p25_value, p50_value, p75_value, blank_percentage."""
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
            assert expected in cols, f"Column '{expected}' missing after init_db"

        raw.close()

    def test_init_db_idempotent_on_second_call(self):
        """Calling init_db twice must not raise (migrations are guarded by column checks)."""
        raw = sqlite3.connect(":memory:")
        raw.row_factory = sqlite3.Row
        wrapper = _NoClose(raw)

        with patch("data.models.get_connection", return_value=wrapper):
            from data.models import init_db
            init_db()
            init_db()   # second call must not raise

        raw.close()
