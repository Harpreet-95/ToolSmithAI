"""
Pure pattern detection helpers for the metadata profiling engine.

No database access, no SQL, no side effects.
All functions accept any iterable of values, skip None and blanks,
and return rates as percentages (0.0–100.0) or None when no data exists.
"""

import re
from collections import Counter
from datetime import datetime, timezone


# ── Compiled regular expressions ──────────────────────────────────────────────

_EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]{2,}$')

_GUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
    r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

_PHONE_CHARS_RE = re.compile(r'^[\+\d\s\-\(\)\.]+$')

_NUMERIC_RE = re.compile(r'^-?\d+(\.\d+)?$')

# Common masking patterns: ****, ***-**-****, XXXX, XXXX-XXXX, [REDACTED]
_MASKED_RE = re.compile(
    r'^\*{2,}[\-\s\d\*]*$'
    r'|^X{3,}[\-\s\dX]*$'
    r'|^\[REDACTED\]$'
    r'|^REDACTED$',
    re.IGNORECASE,
)

# Date string anchors — checked after ISO-8601 fast path
_US_DATE_RE    = re.compile(r'^\d{2}/\d{2}/\d{4}')
_EU_DATE_RE    = re.compile(r'^\d{2}-\d{2}-\d{4}')
_NAMED_DATE_RE = re.compile(r'^[A-Za-z]{3}\s+\d{1,2}')


# ── Private helpers ────────────────────────────────────────────────────────────

def _non_empty(values) -> list[str]:
    """Return values as stripped strings, skipping None and blank entries."""
    result = []
    for v in values:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            result.append(s)
    return result


def _rate(matches: int, total: int) -> float | None:
    """Return matches / total * 100, or None when total is zero."""
    if total == 0:
        return None
    return (matches / total) * 100.0


def _is_phone(v: str) -> bool:
    """Return True when v contains only phone-valid characters and 7–15 digit chars."""
    if not _PHONE_CHARS_RE.match(v):
        return False
    digits = re.sub(r'[^\d]', '', v)
    return 7 <= len(digits) <= 15


def _is_date_string(v: str) -> bool:
    """Return True when v appears to be a date or datetime string."""
    # ISO-8601 fast path covers the majority of enterprise date strings
    try:
        datetime.fromisoformat(v[:26].replace('Z', '+00:00'))
        return True
    except ValueError:
        pass
    return bool(
        _US_DATE_RE.match(v)
        or _EU_DATE_RE.match(v)
        or _NAMED_DATE_RE.match(v)
    )


def _is_masked(v: str) -> bool:
    """Return True when v appears to be a masked or redacted value."""
    if _MASKED_RE.match(v):
        return True
    # Fallback: ≥75% of alphanumeric chars are masking symbols
    alnum = [c for c in v if c.isalnum()]
    if len(alnum) >= 4:
        mask_count = sum(1 for c in alnum if c in '*Xx#')
        return mask_count / len(alnum) >= 0.75
    return False


# ── Public functions ───────────────────────────────────────────────────────────

def email_match_rate(values) -> float | None:
    """Return the percentage of non-empty values that match an email pattern (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _EMAIL_RE.match(v)), len(items))


def phone_match_rate(values) -> float | None:
    """Return the percentage of non-empty values that match a phone pattern (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _is_phone(v)), len(items))


def guid_match_rate(values) -> float | None:
    """Return the percentage of non-empty values that match a UUID/GUID format (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _GUID_RE.match(v)), len(items))


def date_string_rate(values) -> float | None:
    """Return the percentage of non-empty values that appear to be date strings (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _is_date_string(v)), len(items))


def numeric_string_rate(values) -> float | None:
    """Return the percentage of non-empty values that are numeric strings (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _NUMERIC_RE.match(v)), len(items))


def masked_value_rate(values) -> float | None:
    """Return the percentage of non-empty values that appear masked or redacted (0–100)."""
    items = _non_empty(values)
    return _rate(sum(1 for v in items if _is_masked(v)), len(items))


def dominant_pattern(values) -> tuple[str | None, float | None]:
    """Return (most_common_pattern, coverage_pct) after normalising values.

    Normalisation: letters → A, digits → 9, all other characters → X.
    Examples: 'abc123' → 'AAA999', 'john@email.com' → 'AAAAXAAAAAXAAA',
              '555-1234' → '999X9999'.
    Returns (None, None) if values is empty or all entries are blank.
    Coverage is expressed as a percentage (0–100).
    """
    items = _non_empty(values)
    if not items:
        return None, None

    def _to_pattern(v: str) -> str:
        return ''.join(
            'A' if c.isalpha() else '9' if c.isdigit() else 'X'
            for c in v
        )

    patterns = [_to_pattern(v) for v in items]
    top_pattern, top_count = Counter(patterns).most_common(1)[0]
    return top_pattern, (top_count / len(items)) * 100.0
