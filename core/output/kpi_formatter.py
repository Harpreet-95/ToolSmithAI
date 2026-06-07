"""
core/output/kpi_formatter.py

KPI value formatting helpers for ToolSmithAI.

Extracted from core/intelligence/business_kpi_engine.py so that any part of
the stack can format KPI values without importing the full KPI engine.

Public interface:
    format_kpi_value(value, format_type) -> str
"""

from typing import Union


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
