"""
Pure statistical helper functions for the metadata profiling engine.

No database access, no SQL, no side effects.
All functions handle None inputs gracefully and return None where results are undefined.
"""

from datetime import datetime, timezone

from core.profiling.models import CardinalityTier, DataCurrency, RowCountTier


def calculate_percentage(
    part: int | float | None,
    total: int | float | None,
) -> float | None:
    """Return part / total * 100, or None if total is zero or either argument is None."""
    if part is None or total is None or total == 0:
        return None
    return (part / total) * 100.0


def determine_row_count_tier(row_count: int | None) -> RowCountTier | None:
    """Map a row count to a RowCountTier bucket, or None if count is unavailable."""
    if row_count is None:
        return None
    if row_count == 0:
        return RowCountTier.EMPTY
    if row_count < 1_000:
        return RowCountTier.TINY
    if row_count < 100_000:
        return RowCountTier.SMALL
    if row_count < 1_000_000:
        return RowCountTier.MEDIUM
    if row_count < 10_000_000:
        return RowCountTier.LARGE
    return RowCountTier.VERY_LARGE


def determine_cardinality_tier(
    distinct_count: int | None,
    populated_count: int | None,
) -> CardinalityTier | None:
    """Classify column cardinality. Priority: CONSTANT → BINARY → UNIQUE → HIGH → MEDIUM → LOW."""
    if distinct_count is None or populated_count is None or populated_count == 0:
        return None
    if distinct_count == 1:
        return CardinalityTier.CONSTANT
    if distinct_count == 2:
        return CardinalityTier.BINARY
    if distinct_count >= populated_count:
        # distinct >= populated guards against slight timing skew on live DBs
        return CardinalityTier.UNIQUE
    ratio = distinct_count / populated_count
    if ratio > 0.95:
        return CardinalityTier.HIGH
    if ratio > 0.10:
        return CardinalityTier.MEDIUM
    return CardinalityTier.LOW


def calculate_uniqueness_score(
    distinct_count: int | None,
    populated_count: int | None,
) -> float | None:
    """Return distinct / populated (0.0–1.0), or None if either argument is missing."""
    if distinct_count is None or populated_count is None or populated_count == 0:
        return None
    return distinct_count / populated_count


def detect_data_currency(latest_record_date: str | datetime | None) -> DataCurrency:
    """Return ACTIVE (<30 d), RECENT (30–180 d), HISTORICAL (>180 d), or UNKNOWN."""
    if latest_record_date is None:
        return DataCurrency.UNKNOWN

    if isinstance(latest_record_date, str):
        try:
            latest = datetime.fromisoformat(
                latest_record_date.replace('Z', '+00:00')
            )
        except (ValueError, AttributeError):
            return DataCurrency.UNKNOWN
    elif isinstance(latest_record_date, datetime):
        latest = latest_record_date
    else:
        return DataCurrency.UNKNOWN

    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)

    age_days = (datetime.now(timezone.utc) - latest).days

    if age_days < 30:
        return DataCurrency.ACTIVE
    if age_days <= 180:
        return DataCurrency.RECENT
    return DataCurrency.HISTORICAL


def safe_float(value: object) -> float | None:
    """Convert value to float, or return None if conversion fails or value is None."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def summarize_top_values(
    top_values: list[dict],
    total_rows: int | None,
) -> list[dict]:
    """Add a percentage field to each top-value row (expects 'value' and 'row_count' keys)."""
    result = []
    for row in top_values:
        result.append({
            'value':      row.get('value'),
            'row_count':  row.get('row_count'),
            'percentage': calculate_percentage(row.get('row_count'), total_rows),
        })
    return result


def compute_histogram(
    min_val: float,
    max_val: float,
    raw_buckets: list[tuple[int, int]],
    n_buckets: int,
    populated_count: int,
) -> list[dict]:
    """Build a complete histogram from raw SQL bucket counts.

    raw_buckets: [(bucket_idx, row_count), ...] — only non-empty buckets.
    Returns n_buckets entries with lower_bound, upper_bound, row_count, percentage.
    Empty buckets are included with row_count=0.
    When min_val == max_val (constant column) returns a single bucket containing all rows.
    """
    if populated_count <= 0 or n_buckets <= 0:
        return []

    if max_val == min_val:
        return [{
            'lower_bound': min_val,
            'upper_bound': max_val,
            'row_count':   populated_count,
            'percentage':  100.0,
        }]

    bucket_width = (max_val - min_val) / n_buckets

    # Index raw SQL results by bucket index for O(1) lookup.
    bucket_map: dict[int, int] = {}
    for idx, count in raw_buckets:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_buckets:
            bucket_map[i] = bucket_map.get(i, 0) + int(count)

    result = []
    for i in range(n_buckets):
        count = bucket_map.get(i, 0)
        result.append({
            'lower_bound': min_val + i * bucket_width,
            'upper_bound': min_val + (i + 1) * bucket_width,
            'row_count':   count,
            'percentage':  calculate_percentage(count, populated_count),
        })
    return result


def classify_distribution_shape(
    *,
    distinct_count: int | None,
    populated_count: int | None,
    null_percentage: float | None,
    p25_value: str | None,
    p50_value: str | None,
    p75_value: str | None,
) -> str | None:
    """Classify the distribution shape of a numeric column from stored statistics.

    Uses no SQL — reuses already-computed percentiles and cardinality stats.

    Returns one of: 'constant', 'sparse', 'highly_skewed', 'right_skewed',
    'left_skewed', 'symmetric', or None when insufficient data is available.

    Skewness is measured via Bowley's quartile coefficient:
        Q = (P75 - 2*P50 + P25) / (P75 - P25)
    which is symmetric around zero and robust to extreme outliers.
    """
    if populated_count is None or populated_count == 0:
        return None

    # Constant: only one distinct value in the column.
    if distinct_count is not None and distinct_count <= 1:
        return 'constant'

    # Sparse: column is heavily null (> 80% of total rows).
    if null_percentage is not None and null_percentage > 80.0:
        return 'sparse'

    # Parse quartile strings; bail if not all available.
    try:
        p25 = float(p25_value) if p25_value is not None else None
        p50 = float(p50_value) if p50_value is not None else None
        p75 = float(p75_value) if p75_value is not None else None
    except (ValueError, TypeError):
        return None

    if p25 is None or p50 is None or p75 is None:
        return None

    iqr = p75 - p25
    if iqr == 0.0:
        # All quartiles identical → constant-like distribution.
        return 'constant'

    # Bowley quartile skewness: range [-1, +1].
    skewness = (p75 - 2.0 * p50 + p25) / iqr

    if abs(skewness) > 0.5:
        return 'highly_skewed'
    if skewness > 0.1:
        return 'right_skewed'
    if skewness < -0.1:
        return 'left_skewed'
    return 'symmetric'
