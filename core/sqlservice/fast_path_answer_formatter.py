"""
Deterministic answer formatting — Phase 7, Step 6C, Part 5.

Formats a grounded answer directly from core.sqlservice.sql_execution.
SqlExecutionResult's own already-validated, already-bounded rows/columns —
no AI call, no invented claim. Every formatted sentence is a mechanical
restatement of a real returned value or row/column count; nothing here
ever claims a trend, majority, cause, comparison, or insight that was not
directly present in the returned rows (Part 5's own explicit requirement).

Covers Part 5's four named simple shapes:
  1. one scalar count/total/average/minimum/maximum (1 row, 1 column);
  2. one row with several simple named values (1 row, >1 column);
  3. a straightforward list/table (>1 row, few columns);
  4. zero rows.

`is_simple_result()` decides which results this module can safely format
on its own; a caller falls back to core.sqlservice.answer_generation_
service.generate_answer() (reused UNCHANGED, never duplicated) only when
this returns False — Part 5's own "must not be part of the normal simple-
question path" requirement for that fallback.
"""

from __future__ import annotations

import re

from core.config import AI_FIRST_SQL_FAST_PATH_SIMPLE_MAX_COLUMNS
from core.sqlservice.sql_execution import SqlExecutionResult

__all__ = ["is_simple_result", "format_deterministic_answer"]

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD_RE = re.compile(r"[_\-]+")

_COUNT_PREFIXES = ("total", "count", "num", "numof", "sum")
_AVG_PREFIXES = ("avg", "average", "mean")
_MIN_PREFIXES = ("min", "minimum", "earliest", "oldest")
_MAX_PREFIXES = ("max", "maximum", "latest", "newest", "most")


def _split_words(column_name: str) -> list[str]:
    """Splits a column name into lowercase words on camelCase, underscore,
    and hyphen boundaries -- purely mechanical, never a lookup table of
    known business terms."""
    spaced = _NON_WORD_RE.sub(" ", column_name)
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", spaced)
    return [w.lower() for w in spaced.split() if w]


def _humanize_column(column_name: str) -> str:
    words = _split_words(column_name)
    return " ".join(words) if words else column_name


def _format_scalar_value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value == int(value):
            return f"{int(value):,}"
        return f"{value:,.2f}"
    if value is None:
        return "no value"
    return str(value)


def _format_one_scalar(column_name: str, value: object) -> str:
    """Mechanical, prefix-driven phrasing for a single (column, value) pair
    -- never infers what the column "means" beyond its own name's literal
    prefix word (total/count/avg/min/max), and never adds a claim the value
    itself does not already state."""
    words = _split_words(column_name)
    formatted_value = _format_scalar_value(value)
    if not words:
        return f"{column_name}: {formatted_value}."
    first, rest = words[0], " ".join(words[1:])
    if first in _COUNT_PREFIXES and rest:
        return f"There are {formatted_value} {rest}."
    if first in _AVG_PREFIXES and rest:
        return f"The average {rest} is {formatted_value}."
    if first in _MIN_PREFIXES and rest:
        return f"The minimum {rest} is {formatted_value}."
    if first in _MAX_PREFIXES and rest:
        return f"The maximum {rest} is {formatted_value}."
    return f"{_humanize_column(column_name).capitalize()}: {formatted_value}."


def is_simple_result(execution_result: SqlExecutionResult) -> bool:
    """True for every result shape core.sqlservice.fast_path_answer_
    formatter.format_deterministic_answer() can safely handle on its own:
    zero rows, exactly one row (any column count -- a scalar or a single
    named-value row), or multiple rows within a bounded column count (a
    straightforward list/table). False only for a genuinely wide multi-row
    result (more columns than AI_FIRST_SQL_FAST_PATH_SIMPLE_MAX_COLUMNS),
    where a neutral one-line description risks losing information a real
    answer-generation pass over the full row set would not -- the ONLY
    case the fast path optionally falls back to the existing, unchanged
    answer_generation_service for (Part 5's own "only when the result
    cannot be represented safely without it")."""
    if execution_result.row_count == 0:
        return True
    if execution_result.row_count == 1:
        return True
    return len(execution_result.columns) <= AI_FIRST_SQL_FAST_PATH_SIMPLE_MAX_COLUMNS


def format_deterministic_answer(execution_result: SqlExecutionResult) -> str:
    """Formats a grounded, deterministic answer for a result
    is_simple_result() already confirmed this module can handle. Never
    called (by core.sqlservice.ai_first_fast_path_service) for a result
    is_simple_result() returned False for."""
    if execution_result.row_count == 0:
        return "No matching records were found."

    columns = execution_result.columns
    if execution_result.row_count == 1:
        row = execution_result.rows[0]
        if len(columns) == 1:
            return _format_one_scalar(columns[0], row[0])
        parts = [f"{_humanize_column(c).capitalize()}: {_format_scalar_value(v)}" for c, v in zip(columns, row)]
        return ", ".join(parts) + "."

    truncation_note = " (results truncated)" if execution_result.truncated else ""
    return f"Found {execution_result.row_count:,} matching record(s){truncation_note}."
