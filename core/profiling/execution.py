"""
Statistical profiling execution helpers.

Each function accepts a DBAPI2-compatible connection plus a profile object,
executes one or more read-only queries against the source database, and updates
the profile object in-place.  Nothing is persisted here — the caller persists.
"""

import logging
import time
from datetime import datetime, timezone

from core.profiling.models import (
    ColumnProfile, ProfilingConfig, ProfilingDepth,
    ProfilingStatus, TableProfile,
)
from core.profiling.patterns import (
    date_string_rate, dominant_pattern, email_match_rate,
    guid_match_rate, masked_value_rate, numeric_string_rate,
    phone_match_rate,
)
from core.profiling.sql.base import ProfilingQueryBuilder
from core.profiling.statistics import (
    calculate_percentage, calculate_uniqueness_score,
    detect_data_currency, determine_cardinality_tier,
    determine_row_count_tier, safe_float, summarize_top_values,
)

logger = logging.getLogger(__name__)


# ── Type-conversion helpers ────────────────────────────────────────────────────

def _int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _str(v) -> str | None:
    return None if v is None else str(v)


def _to_iso(v) -> str | None:
    """Convert a DB-returned value (datetime, date, or str) to an ISO-8601 string."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, 'isoformat'):          # date objects, etc.
        return v.isoformat()
    return str(v)


def _row_to_dict(cursor, row) -> dict:
    """Convert a DBAPI2 row to a lowercased-key dict using cursor.description."""
    keys = [col[0].lower() for col in cursor.description]
    return dict(zip(keys, row))


# ── Public execution functions ─────────────────────────────────────────────────

def profile_table_statistics(
    conn,
    tp: TableProfile,
    config: ProfilingConfig,
    builder: ProfilingQueryBuilder,
) -> TableProfile:
    """Execute row-count and date-range queries; update TableProfile in-place."""
    t0 = time.monotonic()
    errors = 0

    # ── Exact row count ────────────────────────────────────────────────────────
    try:
        cur = conn.execute(builder.build_row_count_query(tp.table_fqn))
        row = cur.fetchone()
        tp.exact_row_count = _int(row[0]) if row else None
        tp.row_count_tier  = determine_row_count_tier(tp.exact_row_count)
    except Exception:
        logger.error("Row count query failed for %s", tp.table_fqn)
        errors += 1

    # ── Date range (only when a date column was identified) ────────────────────
    if tp.has_date_column and tp.date_column_name and tp.exact_row_count:
        try:
            cur = conn.execute(
                builder.build_date_range_query(tp.table_fqn, tp.date_column_name)
            )
            row = cur.fetchone()
            if row:
                tp.earliest_record = _to_iso(row[0])
                tp.latest_record   = _to_iso(row[1])

                if tp.earliest_record and tp.latest_record:
                    try:
                        early = datetime.fromisoformat(tp.earliest_record.replace('Z', '+00:00'))
                        late  = datetime.fromisoformat(tp.latest_record.replace('Z', '+00:00'))
                        tp.data_span_days = max(0, (late - early).days)
                    except ValueError:
                        pass

                tp.data_currency = detect_data_currency(tp.latest_record)
        except Exception:
            logger.error("Date range query failed for %s.%s", tp.table_fqn, tp.date_column_name)
            errors += 1

    tp.profiling_depth    = ProfilingDepth.STATISTICAL
    tp.profiling_status   = ProfilingStatus.PARTIAL if errors else ProfilingStatus.COMPLETE
    tp.profiling_duration_ms = int((time.monotonic() - t0) * 1000)
    tp.profiled_at        = datetime.now(timezone.utc).isoformat()
    return tp


def profile_column_statistics(
    conn,
    cp: ColumnProfile,
    config: ProfilingConfig,
    builder: ProfilingQueryBuilder,
) -> ColumnProfile:
    """Execute the column statistics query; update ColumnProfile in-place."""
    t0 = time.monotonic()
    try:
        sql = builder.build_column_stats_query(cp.table_fqn, cp.column_name, cp.data_type)
        cur = conn.execute(sql)
        row = cur.fetchone()
        if row is None:
            cp.profiling_status = ProfilingStatus.PARTIAL
            return cp

        d  = _row_to_dict(cur, row)
        total     = _int(d.get('total_rows'))
        populated = _int(d.get('populated_count'))
        null_cnt  = _int(d.get('null_count'))
        distinct  = _int(d.get('distinct_count'))

        cp.null_count           = null_cnt
        cp.populated_count      = populated
        cp.null_percentage      = calculate_percentage(null_cnt, total)
        cp.populated_percentage = calculate_percentage(populated, total)
        cp.distinct_count       = distinct
        cp.distinct_percentage  = calculate_percentage(distinct, populated)
        cp.uniqueness_score     = calculate_uniqueness_score(distinct, populated)
        cp.cardinality_tier     = determine_cardinality_tier(distinct, populated)
        cp.min_value            = _str(d.get('min_value'))
        cp.max_value            = _str(d.get('max_value'))

        dt = cp.data_type.upper()
        if dt == 'TEXT':
            cp.avg_length          = safe_float(d.get('avg_length'))
            cp.min_length          = _int(d.get('min_length'))
            cp.max_length_observed = _int(d.get('max_length_observed'))
            cp.empty_string_count  = _int(d.get('empty_string_count'))
            # Derive blank_percentage from stored counts (no extra query)
            if cp.empty_string_count is not None and total:
                cp.blank_percentage = (cp.empty_string_count / total) * 100.0
        elif dt in ('INTEGER', 'DECIMAL'):
            cp.mean_value    = safe_float(d.get('mean_value'))
            cp.std_deviation = safe_float(d.get('std_deviation'))
            cp.zero_count    = _int(d.get('zero_count'))
            # Derive variance from std_deviation (not persisted to DB)
            if cp.std_deviation is not None:
                cp.variance = cp.std_deviation ** 2
        elif dt == 'BOOLEAN':
            cp.zero_count = _int(d.get('zero_count'))

        cp.profiling_depth  = ProfilingDepth.STATISTICAL
        cp.profiling_status = ProfilingStatus.COMPLETE

    except Exception:
        logger.error("Column stats failed for %s.%s", cp.table_fqn, cp.column_name)
        cp.profiling_status = ProfilingStatus.FAILED
    finally:
        cp.profiling_duration_ms = int((time.monotonic() - t0) * 1000)

    return cp


def profile_top_values(
    conn,
    cp: ColumnProfile,
    config: ProfilingConfig,
    builder: ProfilingQueryBuilder,
) -> ColumnProfile:
    """Execute the top-N values query; update ColumnProfile in-place."""
    t0 = time.monotonic()
    try:
        sql = builder.build_top_values_query(
            cp.table_fqn, cp.column_name,
            limit=config.max_top_values,
        )
        cur  = conn.execute(sql)
        rows = cur.fetchall()

        if rows:
            raw = [
                {'value': _str(r[0]), 'row_count': _int(r[1]) or 0}
                for r in rows
            ]
            # Use populated_count as denominator; it was set by profile_column_statistics.
            cp.top_values = summarize_top_values(raw, cp.populated_count)

            if cp.populated_count:
                covered = sum(r['row_count'] for r in raw)
                cp.top_values_coverage = calculate_percentage(covered, cp.populated_count)

    except Exception:
        logger.error("Top values query failed for %s.%s", cp.table_fqn, cp.column_name)
    finally:
        cp.profiling_duration_ms = (cp.profiling_duration_ms or 0) + int((time.monotonic() - t0) * 1000)

    return cp


def profile_column_percentiles(
    conn,
    cp: ColumnProfile,
    config: ProfilingConfig,
    builder: ProfilingQueryBuilder,
) -> ColumnProfile:
    """Execute the percentile query for numeric columns; update ColumnProfile in-place.

    Populates p5_value, p25_value, p50_value (median), p75_value, p95_value.
    Skipped for non-numeric types and columns with no populated rows so we
    never issue a query that would return no useful result.
    Must be called after profile_column_statistics so populated_count is known.
    """
    dt = cp.data_type.upper()
    if dt not in ('INTEGER', 'DECIMAL'):
        return cp
    if not cp.populated_count:
        return cp

    t0 = time.monotonic()
    try:
        sql = builder.build_percentile_query(cp.table_fqn, cp.column_name)
        cur = conn.execute(sql)
        row = cur.fetchone()
        if row:
            d = _row_to_dict(cur, row)
            cp.p5_value  = _str(d.get('p5_value'))
            cp.p25_value = _str(d.get('p25_value'))
            cp.p50_value = _str(d.get('p50_value'))
            cp.p75_value = _str(d.get('p75_value'))
            cp.p95_value = _str(d.get('p95_value'))
    except Exception:
        logger.error("Percentile query failed for %s.%s", cp.table_fqn, cp.column_name)
    finally:
        cp.profiling_duration_ms = (cp.profiling_duration_ms or 0) + int((time.monotonic() - t0) * 1000)

    return cp


def profile_sample_values(
    conn,
    cp: ColumnProfile,
    config: ProfilingConfig,
    builder: ProfilingQueryBuilder,
) -> ColumnProfile:
    """Execute the sample values query, run pattern detection; update ColumnProfile in-place.

    PII columns are fully skipped — no sample values are stored and no pattern
    rates are computed to avoid processing sensitive data.
    """
    if cp.pii_name_heuristic:
        return cp

    t0 = time.monotonic()
    try:
        # Try 5% page-sample first; retry at 100% for small tables that return 0 rows.
        rows = []
        for pct in (5, 100):
            sql  = builder.build_sample_values_query(
                cp.table_fqn, cp.column_name,
                limit=config.max_sample_values,
                sample_percent=pct,
            )
            rows = conn.execute(sql).fetchall()
            if rows:
                break

        raw_values = [str(r[0]) for r in rows if r[0] is not None]
        cp.sample_values = raw_values

        if raw_values:
            dt = cp.data_type.upper()

            # Detect masking and GUID regardless of type
            cp.masked_value_rate = masked_value_rate(raw_values)
            cp.guid_match_rate   = guid_match_rate(raw_values)

            if dt == 'TEXT':
                cp.email_match_rate    = email_match_rate(raw_values)
                cp.phone_match_rate    = phone_match_rate(raw_values)
                cp.date_string_rate    = date_string_rate(raw_values)
                cp.numeric_string_rate = numeric_string_rate(raw_values)

            patt, coverage = dominant_pattern(raw_values)
            cp.dominant_pattern = patt
            cp.pattern_coverage = coverage

        cp.profiling_depth  = ProfilingDepth.FULL
        cp.profiling_status = ProfilingStatus.COMPLETE

    except Exception:
        logger.error("Sample values query failed for %s.%s", cp.table_fqn, cp.column_name)
    finally:
        cp.profiling_duration_ms = (cp.profiling_duration_ms or 0) + int((time.monotonic() - t0) * 1000)

    return cp
