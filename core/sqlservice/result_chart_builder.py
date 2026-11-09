"""
Deterministic, zero-AI-call chart/KPI specification builder — Phase 7,
Step 6G, Part 3.

Mirrors core.sqlservice.result_insight_builder's own shape-detection
convention and guards (same module family, deliberately NOT importing its
private helpers -- see that module's own docstring on why each module here
owns its small helpers independently). Builds at most one chart (or one
KPI) specification directly from an already-executed, already-bounded
result's own rows/columns -- no provider call, no prompt, nothing beyond
arithmetic and column-shape inspection over values the database itself
already returned.

Accepts any object exposing `.columns` (tuple[str, ...]), `.rows`
(tuple[tuple, ...]), `.row_count` (int), `.truncated` (bool) -- this is a
structural (duck-typed) contract shared by both
core.sqlservice.sql_execution.SqlExecutionResult and
core.sqlservice.ai_first_result.AiFirstOrchestrationResult,
so the SAME builder serves both the AI-first fast path and the AI-first
multi-call pipeline without threading a new field through either
orchestrator -- the adapter that already reads result.columns/.rows for
the result-preview table is the single call site.

Returned shape is exactly what frontend/src/components/ChartSection.jsx
already accepts (reused unchanged, no new frontend chart component):
  {"chart_type": "bar"|"donut"|"line"|"scatter", "labels": [...], "series": [{"name": str, "data": [...]}]}
  {"chart_type": "scatter", "x_label": str, "y_label": str, "points": [{"x": num, "y": num}, ...]}
or None for a single-scalar result, a KPI dict is returned instead via
build_deterministic_kpi() -- the two are mutually exclusive callers in
api/v1/ai_first_composer_adapter.py (chart vs kpi field).

Guards, applied in order, any one of which suppresses the chart for this
result (never a partial/misleading chart when the required evidence is
unavailable):
  1. Zero rows -- nothing to plot.
  2. execution_result.truncated -- a truncated result cannot support a
     chart that would visually imply it represents the full population.
  3. Exactly one row -- handled by build_deterministic_kpi() instead
     (a single data point is a KPI, never a one-bar chart).
  4. The column shape is not one of the two this module understands
     (exactly one numeric + one date-like column, exactly one numeric +
     one non-numeric/non-date column, or exactly two numeric columns) --
     ambiguous/detail/PII-heavy multi-column shapes are left alone (table
     only), never guessed at.
  5. A degenerate two-numeric-column case (no variance in one dimension,
     or fewer than 3 distinct points) is not "meaningful" for scatter and
     is suppressed rather than shown.
"""
from __future__ import annotations

import datetime
import re
from typing import Sequence

__all__ = ["build_deterministic_chart", "build_deterministic_kpi"]

_MAX_CHART_CATEGORIES = 25
_MIN_SCATTER_POINTS = 3
_DONUT_MAX_CATEGORIES = 6
_MIN_MULTI_MEASURES = 2
_MAX_MULTI_MEASURES = 4

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_WORD_RE = re.compile(r"[_\-]+")
_NON_ADDITIVE_TOKENS = frozenset({
    "avg", "average", "mean", "min", "minimum", "max", "maximum", "rate", "percent", "pct", "ratio",
})
_DATE_TOKENS = frozenset({"date", "month", "period", "year", "week", "day"})
_MONTH_STRING_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")


def _words(column_name: str) -> list[str]:
    spaced = _NON_WORD_RE.sub(" ", column_name or "")
    spaced = _CAMEL_BOUNDARY_RE.sub(" ", spaced)
    return [w.lower() for w in spaced.split() if w]


def _humanize(column_name: str) -> str:
    words = _words(column_name)
    return " ".join(w.capitalize() for w in words) if words else str(column_name)


def _is_additive_safe(column_name: str) -> bool:
    return not (_NON_ADDITIVE_TOKENS & set(_words(column_name)))


def _looks_like_date_column(column_name: str) -> bool:
    return bool(_DATE_TOKENS & set(_words(column_name)))


def _looks_like_surrogate_id_column(column_name: str) -> bool:
    """Purely a naming-pattern check (mirrors result_insight_builder's own
    identical rule) -- a numeric surrogate key alongside a genuine
    category/date+measure pair must not block shape detection just because
    it is ALSO numeric."""
    words = _words(column_name)
    return bool(words) and words[-1] == "id"


def _is_numeric_value(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_date_value(value: object) -> bool:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return True
    if isinstance(value, str) and _MONTH_STRING_RE.match(value.strip()):
        return True
    return False


def _calendar_part(column_name: str) -> str | None:
    """Returns 'year', 'month', or 'day' when the column's LAST name-word
    is exactly that token (mirrors _looks_like_surrogate_id_column's own
    words[-1] convention) -- 'EnrollmentYear'/'SignupMonth' match, a plain
    'period'/'date'/'week' column does not (those are already handled by
    _column_kind's value-based date detection)."""
    words = _words(column_name)
    if not words:
        return None
    return words[-1] if words[-1] in ("year", "month", "day") else None


def _values_in_range(values: Sequence[object], lo: int, hi: int) -> bool:
    non_null = [v for v in values if v is not None]
    if not non_null:
        return False
    return all(_is_numeric_value(v) and lo <= v <= hi for v in non_null)


def _detect_year_month_columns(
    columns: Sequence[str], rows: Sequence[Sequence[object]], usable_positions: Sequence[int],
) -> tuple[int, int, int | None] | None:
    """Finds exactly one YEAR-named column (integer values in [1000, 9999])
    and exactly one MONTH-named column (integer values in [1, 12]) among
    usable_positions, and optionally a DAY-named column (integer values in
    [1, 31]) -- a very common GROUP BY YEAR(date), MONTH(date) shape that
    returns calendar parts as separate integer columns instead of one date
    column. Returns (year_pos, month_pos, day_pos_or_None), or None when no
    such year+month pair exists (never guesses from a partial match)."""
    year_pos = month_pos = day_pos = None
    for i in usable_positions:
        part = _calendar_part(columns[i])
        if part is None:
            continue
        values = [row[i] for row in rows]
        if part == "year" and year_pos is None and _values_in_range(values, 1000, 9999):
            year_pos = i
        elif part == "month" and month_pos is None and _values_in_range(values, 1, 12):
            month_pos = i
        elif part == "day" and day_pos is None and _values_in_range(values, 1, 31):
            day_pos = i
    if year_pos is not None and month_pos is not None:
        return year_pos, month_pos, day_pos
    return None


def _synthesize_period_column(
    columns: Sequence[str], rows: Sequence[Sequence[object]],
    usable_positions: Sequence[int], combo: tuple[int, int, int | None],
) -> tuple[list[str], list[list[object]]]:
    """Collapses a detected (year, month, [day]) column combo into a single
    synthetic leading date column, dropping the consumed columns and
    keeping every other usable column as-is -- turns a 3+-column split-date
    shape into the same (date, measure...) shape _column_kind/the existing
    branches below already understand, with zero new chart logic."""
    year_pos, month_pos, day_pos = combo
    consumed = {year_pos, month_pos} | ({day_pos} if day_pos is not None else set())
    keep_positions = [i for i in usable_positions if i not in consumed]
    period_label = "Date" if day_pos is not None else "Month"
    new_columns = [period_label] + [columns[i] for i in keep_positions]
    new_rows: list[list[object]] = []
    for row in rows:
        y, m = row[year_pos], row[month_pos]
        d: object = None
        if y is not None and m is not None:
            day_val = row[day_pos] if day_pos is not None and row[day_pos] is not None else 1
            try:
                d = datetime.date(int(y), int(m), int(day_val))
            except (ValueError, TypeError):
                d = None
        new_rows.append([d] + [row[i] for i in keep_positions])
    return new_columns, new_rows


def _column_kind(values: Sequence[object], column_name: str) -> str:
    """Returns 'numeric', 'date', or 'other' using the ACTUAL returned
    values first -- the column name is consulted only as a tie-breaker for
    an all-NULL column."""
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "date" if _looks_like_date_column(column_name) else "other"
    if all(_is_numeric_value(v) for v in non_null):
        return "numeric"
    if all(_is_date_value(v) for v in non_null):
        return "date"
    return "other"


def _fmt_date_label(value: object) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _category_chart(
    columns: Sequence[str], rows: Sequence[Sequence[object]], label_pos: int, measure_pos: int,
) -> dict | None:
    label_name, measure_name = columns[label_pos], columns[measure_pos]
    pairs: list[tuple[object, float]] = []
    for row in rows:
        label, value = row[label_pos], row[measure_pos]
        if label is None or value is None:
            continue
        if isinstance(label, str) and not label.strip():
            continue
        pairs.append((label, value))
    if len(pairs) < 2 or len(pairs) > _MAX_CHART_CATEGORIES:
        return None

    labels = [str(lbl) for lbl, _ in pairs]
    values = [float(v) for _, v in pairs]
    measure_label = _humanize(measure_name) or "Value"

    is_composition = (
        len(pairs) <= _DONUT_MAX_CATEGORIES
        and _is_additive_safe(measure_name)
        and all(v >= 0 for v in values)
        and sum(values) > 0
    )
    chart_type = "donut" if is_composition else "bar"
    return {
        "chart_type": chart_type,
        "title": f"{measure_label} by {_humanize(label_name) or 'category'}",
        "labels": labels,
        "series": [{"name": measure_label, "data": values}],
    }


def _time_series_chart(
    columns: Sequence[str], rows: Sequence[Sequence[object]], date_pos: int, measure_pos: int,
) -> dict | None:
    date_name, measure_name = columns[date_pos], columns[measure_pos]
    points: list[tuple[object, float]] = []
    for row in rows:
        d, v = row[date_pos], row[measure_pos]
        if d is None or v is None:
            continue
        points.append((d, v))
    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    measure_label = _humanize(measure_name) or "Value"
    return {
        "chart_type": "line",
        "title": f"{measure_label} over {_humanize(date_name) or 'time'}",
        "labels": [_fmt_date_label(d) for d, _ in points],
        "series": [{"name": measure_label, "data": [float(v) for _, v in points]}],
    }


def _multi_measure_time_series_chart(
    columns: Sequence[str], rows: Sequence[Sequence[object]], date_pos: int, measure_positions: Sequence[int],
) -> dict | None:
    """A date dimension alongside two or more numeric measures (e.g. a
    payment COUNT and a payment TOTAL in the same GROUP BY row) -- a very
    common, generic shape that the single-measure _time_series_chart above
    cannot represent without silently dropping one measure. Renders every
    measure as its own series on the SAME existing grouped_line chart_type
    (frontend/src/components/ChartSection.jsx's GroupedLineChart, already
    built and already reachable for other callers -- reused unchanged,
    never stacked/summed, so differing units across measures is never a
    correctness risk)."""
    date_name = columns[date_pos]
    points: list[tuple[object, Sequence[object]]] = []
    for row in rows:
        d = row[date_pos]
        if d is None:
            continue
        points.append((d, row))
    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    series = []
    for pos in measure_positions:
        measure_name = columns[pos]
        data = [float(v) if v is not None else None for _, row in points for v in (row[pos],)]
        series.append({"name": _humanize(measure_name) or measure_name, "data": data})

    measure_labels = ", ".join(s["name"] for s in series)
    return {
        "chart_type": "grouped_line",
        "title": f"{measure_labels} over {_humanize(date_name) or 'time'}",
        "labels": [_fmt_date_label(d) for d, _ in points],
        "series": series,
    }


def _scatter_chart(
    columns: Sequence[str], rows: Sequence[Sequence[object]], x_pos: int, y_pos: int,
) -> dict | None:
    x_name, y_name = columns[x_pos], columns[y_pos]
    points: list[tuple[float, float]] = []
    for row in rows:
        x, y = row[x_pos], row[y_pos]
        if x is None or y is None:
            continue
        points.append((float(x), float(y)))
    if len(points) < _MIN_SCATTER_POINTS:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # "Only when meaningful" (Part 3's own requirement) -- a scatter where
    # every point shares the same x or the same y carries no visual
    # relationship to show, so it is suppressed rather than rendered flat.
    if max(xs) == min(xs) or max(ys) == min(ys):
        return None

    return {
        "chart_type": "scatter",
        "title": f"{_humanize(y_name)} vs {_humanize(x_name)}",
        "x_label": _humanize(x_name),
        "y_label": _humanize(y_name),
        "points": [{"x": x, "y": y} for x, y in points],
    }


def build_deterministic_chart(execution_result) -> dict | None:
    """Returns one grounded chart spec (ChartSection.jsx shape), or None
    whenever any guard above applies or the shape is not one this module
    understands. Never raises -- a defensive catch-all returns None rather
    than risk surfacing a misleading chart (this module's own analysis
    must never be able to fail a request that otherwise succeeded)."""
    try:
        if execution_result is None or execution_result.row_count < 2 or execution_result.truncated:
            return None
        columns = list(execution_result.columns)
        rows = execution_result.rows
        if len(columns) < 2:
            return None

        usable_positions = [i for i in range(len(columns)) if not _looks_like_surrogate_id_column(columns[i])]
        if not usable_positions:
            return None

        combo = _detect_year_month_columns(columns, rows, usable_positions)
        if combo is not None:
            columns, rows = _synthesize_period_column(columns, rows, usable_positions, combo)
            usable_positions = list(range(len(columns)))

        kinds = {
            i: _column_kind([row[i] for row in rows], columns[i])
            for i in usable_positions
        }
        numeric_positions = [i for i in usable_positions if kinds[i] == "numeric"]
        date_positions = [i for i in usable_positions if kinds[i] == "date"]
        other_positions = [i for i in usable_positions if kinds[i] == "other"]

        if len(usable_positions) == 2:
            if len(numeric_positions) == 1 and len(date_positions) == 1:
                return _time_series_chart(columns, rows, date_positions[0], numeric_positions[0])
            if len(numeric_positions) == 1 and len(other_positions) == 1:
                return _category_chart(columns, rows, other_positions[0], numeric_positions[0])
            if len(numeric_positions) == 2:
                return _scatter_chart(columns, rows, numeric_positions[0], numeric_positions[1])
            return None

        # A date dimension plus 2-4 numeric measures (e.g. a payment COUNT
        # and a payment TOTAL in the same row) -- every other column
        # combination with more than 2 usable columns stays ambiguous
        # (table only), matching this module's own "never guess" contract.
        if (
            len(date_positions) == 1
            and len(other_positions) == 0
            and _MIN_MULTI_MEASURES <= len(numeric_positions) <= _MAX_MULTI_MEASURES
        ):
            return _multi_measure_time_series_chart(columns, rows, date_positions[0], numeric_positions)
        return None
    except Exception:  # noqa: BLE001 -- pure analysis must never break a successful answer
        return None


def build_deterministic_kpi(execution_result) -> dict | None:
    """Returns {"label": str, "value": number} for a genuine single-scalar
    result (exactly one row, exactly one usable numeric column after
    excluding surrogate-ID-named columns), or None otherwise -- never
    invents a KPI for a one-row-multi-column detail record (that stays
    table-only)."""
    try:
        if execution_result is None or execution_result.row_count != 1 or execution_result.truncated:
            return None
        columns = list(execution_result.columns)
        usable_positions = [i for i in range(len(columns)) if not _looks_like_surrogate_id_column(columns[i])]
        if len(usable_positions) != 1:
            return None
        pos = usable_positions[0]
        value = execution_result.rows[0][pos]
        if not _is_numeric_value(value):
            return None
        return {"label": _humanize(columns[pos]) or "Value", "value": value}
    except Exception:  # noqa: BLE001
        return None
