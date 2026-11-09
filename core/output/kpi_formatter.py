"""
core/output/kpi_formatter.py

KPI value formatting helpers for ToolSmithAI.

Extracted from core/intelligence/business_kpi_engine.py so that any part of
the stack can format KPI values without importing the full KPI engine.

Public interface:
    format_kpi_value(value, format_type) -> str
    format_kpi_display_label(label) -> str
    format_dataset_display_name(filename) -> str
"""

from typing import Union

_BUSINESS_ACRONYMS: frozenset[str] = frozenset({"EV", "KPI", "ROI", "EBITDA", "ESG", "API"})

_PREFIX_EXPANSIONS: dict[str, str] = {
    "avg": "Average",
    "average": "Average",
    "total": "Total",
    "sum": "Total",
    "count": "Count",
    "max": "Maximum",
    "min": "Minimum",
    "num": "Number of",
}


def _fmt_currency(value: float) -> str:
    try:
        abs_v = abs(value)
        sign  = "-" if value < 0 else ""
        if abs_v >= 1_000_000_000:
            return f"{sign}${abs_v / 1_000_000_000:.2f}B"
        if abs_v >= 1_000_000:
            return f"{sign}${abs_v / 1_000_000:.2f}M"
        if abs_v >= 1_000:
            return f"{sign}${abs_v / 1_000:.1f}K"
        return f"{sign}${abs_v:,.0f}"
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _fmt_number(value: float) -> str:
    try:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        if abs(value) >= 1_000:
            return f"{value:,.0f}"
        return f"{value:,.0f}"
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _fmt_percent(value: float) -> str:
    try:
        return f"{value:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_decimal(value: float) -> str:
    try:
        return f"{value:.3f}"
    except (TypeError, ValueError):
        return str(value)


def format_kpi_value(value: Union[float, int], format_type: str) -> str:
    """Format a KPI value according to the given format type.

    format_type: "currency" | "percent" | "number" | "decimal"
    Returns a display string. Never raises.
    """
    if format_type == "currency":
        return _fmt_currency(value)
    if format_type == "percent":
        return _fmt_percent(value)
    if format_type == "number":
        return _fmt_number(value)
    return _fmt_decimal(value)


def format_kpi_display_label(label: str) -> str:
    """Normalize a KPI label for customer-facing display.

    - Removes duplicated prefixes ("Avg Avg" → "Average")
    - Converts snake_case to human-readable words
    - Drops trailing _usd (currency symbol on the value is sufficient)
    - Expands common abbreviation prefixes (avg → Average, total → Total)
    - Title-cases remaining words
    - Preserves known business acronyms (EV, KPI, ROI, EBITDA, ESG, API)
    """
    if not label:
        return label

    parts = label.replace("_", " ").replace("-", " ").split()
    if not parts:
        return label

    # Drop trailing "usd" — value already carries the $ symbol
    if parts[-1].lower() == "usd":
        parts = parts[:-1]
    if not parts:
        return label

    # Collapse consecutive avg/average duplicates at the head
    while len(parts) >= 2 and parts[0].lower() in _PREFIX_EXPANSIONS and parts[1].lower() in _PREFIX_EXPANSIONS:
        parts = parts[1:]

    if not parts:
        return label

    # Expand the leading prefix abbreviation (avg → Average, etc.)
    if parts[0].lower() in _PREFIX_EXPANSIONS:
        parts[0] = _PREFIX_EXPANSIONS[parts[0].lower()]

    result: list[str] = []
    for p in parts:
        if p.upper() in _BUSINESS_ACRONYMS:
            result.append(p.upper())
        else:
            result.append(p.capitalize())

    return " ".join(result)


def format_dataset_display_name(filename: str) -> str:
    """Format a dataset filename for customer-facing display.

    - Removes file extension
    - Replaces underscores and dashes with spaces
    - Normalizes spacing
    - Title-cases output with business acronym preservation
    """
    if not filename:
        return filename

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    parts = stem.replace("_", " ").replace("-", " ").split()
    if not parts:
        return filename

    result: list[str] = []
    for p in parts:
        if p.upper() in _BUSINESS_ACRONYMS:
            result.append(p.upper())
        else:
            result.append(p.capitalize())

    return " ".join(result)
