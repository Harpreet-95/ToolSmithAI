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
