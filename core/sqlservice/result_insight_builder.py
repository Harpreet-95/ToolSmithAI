"""
Deterministic, zero-AI-call result insights — Phase 7, Step 6E, Part 3.

Builds a small number of AnswerInsight records directly from an already-
executed core.sqlservice.sql_execution.SqlExecutionResult's own rows/columns
-- no provider call, no prompt, nothing beyond arithmetic over values the
database itself already returned. Every insight this module emits is
mechanically recomputable from execution_result.rows; nothing here ever
infers a cause, trend beyond a literal two-period comparison, prediction, or
business explanation.

Small, per-module helpers only (no shared private import from
core.sqlservice.fast_path_answer_formatter — same "small validators/helpers
live per-module" convention that module's own docstring documents).

Guards, applied in order, any one of which suppresses ALL insights for this
result (never a partial/best-guess insight when the required evidence is
unavailable):
  1. Zero rows -- nothing to compare; the caller's own deterministic
     zero-row answer already states this.
  2. execution_result.truncated -- a truncated result cannot support any
     claim about a GLOBAL total, maximum, minimum, or distribution (Part 3's
     own explicit requirement); rather than guess which claims would still
     be safe, this module suppresses everything for a truncated result.
  3. Exactly one row -- the caller's own deterministic single-row answer
     already states every value directly; there is nothing to compare.
  4. The column shape is not one of the two this module understands
     (exactly one numeric + one date-like column, or exactly one numeric +
     one non-numeric/non-date column) -- ambiguous shapes (more than one
     numeric column, no numeric column, more than two columns) are left
     alone rather than guessed at.
"""
from __future__ import annotations

import datetime
import re
from typing import Sequence

from core.sqlservice.answer_generation_service import AnswerInsight, EvidenceColumnReference

__all__ = ["build_deterministic_insights"]

_PROVENANCE = "deterministic_result_analysis"
_MAX_INSIGHTS = 4

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
    return " ".join(words) if words else str(column_name)


def _is_additive_safe(column_name: str) -> bool:
    return not (_NON_ADDITIVE_TOKENS & set(_words(column_name)))


def _looks_like_date_column(column_name: str) -> bool:
    return bool(_DATE_TOKENS & set(_words(column_name)))


def _looks_like_surrogate_id_column(column_name: str) -> bool:
    """True for a technical identifier column (TrackID, StudentID, ID, ...)
    -- purely a naming-pattern check (mirrors this repo's own primary-key/
    mapping-token conventions, e.g. core.sqlservice.retrieval_documents'
    own _MAPPING_TOKENS), never a business-relationship inference. A
    numeric surrogate key sitting alongside a genuine category+measure pair
    (e.g. SELECT TrackID, ProgramDescription, StudentCount FROM ...) must
    not block insight generation just because it is ALSO numeric."""
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
    _column_kind's value-based date detection). Mirrors
    core.sqlservice.result_chart_builder's identical helper."""
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
    shape into the same (date, measure) 2-column shape this module already
    understands, with zero new insight logic. Mirrors
    core.sqlservice.result_chart_builder's identical helper."""
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
    """Returns 'numeric', 'date', or 'other' for one column, using the
    ACTUAL returned values first (never guessed from the name alone) --
    the column name is consulted only as a tie-breaker for an all-NULL
    numeric-shaped column that also reads as a date-ish name."""
    non_null = [v for v in values if v is not None]
    if not non_null:
        return "date" if _looks_like_date_column(column_name) else "other"
    if all(_is_numeric_value(v) for v in non_null):
        return "numeric"
    if all(_is_date_value(v) for v in non_null):
        return "date"
    return "other"


def _fmt_value(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}" if value != int(value) else f"{int(value):,}"
    if value is None:
        return "no value"
    return str(value)


def _fmt_date(value: object) -> str:
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _evidence_columns(*positions_and_names: tuple[int, str]) -> tuple:
    return tuple(EvidenceColumnReference(position=p, name=n) for p, n in positions_and_names)


def _category_measure_insights(
    columns: Sequence[str], rows: Sequence[Sequence[object]], label_pos: int, measure_pos: int,
) -> list[AnswerInsight]:
    label_name, measure_name = columns[label_pos], columns[measure_pos]
    measure_word = _humanize(measure_name) or "value"

    pairs: list[tuple[int, object, object]] = []  # (1-indexed row number, label, value)
    for i, row in enumerate(rows, start=1):
        label, value = row[label_pos], row[measure_pos]
        if label is None or value is None:
            continue
        if isinstance(label, str) and not label.strip():
            continue  # a blank label is not a meaningful named category
        pairs.append((i, label, value))
    if len(pairs) < 2:
        return []

    insights: list[AnswerInsight] = []
    evidence_cols = _evidence_columns((label_pos, label_name), (measure_pos, measure_name))

    max_value = max(v for _, _, v in pairs)
    min_value = min(v for _, _, v in pairs)
    top_rows = [(i, lbl) for i, lbl, v in pairs if v == max_value]
    bottom_rows = [(i, lbl) for i, lbl, v in pairs if v == min_value]
    if max_value != min_value:
        top_desc = " (tied)" if len(top_rows) > 1 else ""
        bottom_desc = " (tied)" if len(bottom_rows) > 1 else ""
        top_labels = ", ".join(str(lbl) for _, lbl in top_rows[:3])
        bottom_labels = ", ".join(str(lbl) for _, lbl in bottom_rows[:3])
        insights.append(AnswerInsight(
            title="Highest and lowest",
            finding=(
                f"{top_labels} has the highest {measure_word} ({_fmt_value(max_value)}){top_desc}, and "
                f"{bottom_labels} has the lowest ({_fmt_value(min_value)}){bottom_desc}."
            ),
            evidence_row_numbers=tuple(sorted({i for i, _ in top_rows} | {i for i, _ in bottom_rows})),
            evidence_columns=evidence_cols, provenance=_PROVENANCE,
        ))

    ranked = sorted(pairs, key=lambda p: p[2], reverse=True)
    if len(ranked) >= 2 and ranked[0][2] != ranked[1][2]:
        diff = ranked[0][2] - ranked[1][2]
        insights.append(AnswerInsight(
            title="Gap between top two",
            finding=(
                f"{ranked[0][1]} leads {ranked[1][1]} by {_fmt_value(diff)} {measure_word}"
                f" ({_fmt_value(ranked[0][2])} vs {_fmt_value(ranked[1][2])})."
            ),
            evidence_row_numbers=(ranked[0][0], ranked[1][0]),
            evidence_columns=evidence_cols, provenance=_PROVENANCE,
        ))

    if _is_additive_safe(measure_name):
        total = sum(v for _, _, v in pairs)
        if total != 0 and max_value != 0:
            pct = (max_value / total) * 100.0
            top_labels = ", ".join(str(lbl) for _, lbl in top_rows[:3])
            insights.append(AnswerInsight(
                title="Share of total",
                finding=f"{top_labels} accounts for {pct:.1f}% of the total {measure_word} ({_fmt_value(total)}).",
                evidence_row_numbers=tuple(i for i, _ in top_rows),
                evidence_columns=evidence_cols, provenance=_PROVENANCE,
            ))

    distinct_labels = len({lbl for _, lbl, _ in pairs})
    if distinct_labels < len(pairs):
        insights.append(AnswerInsight(
            title="Distinct categories",
            finding=f"{len(pairs)} rows span only {distinct_labels} distinct {_humanize(label_name) or 'categories'}.",
            evidence_row_numbers=tuple(i for i, _, _ in pairs),
            evidence_columns=evidence_cols, provenance=_PROVENANCE,
        ))

    return insights


def _time_series_insight(
    columns: Sequence[str], rows: Sequence[Sequence[object]], date_pos: int, measure_pos: int,
) -> list[AnswerInsight]:
    date_name, measure_name = columns[date_pos], columns[measure_pos]
    measure_word = _humanize(measure_name) or "value"

    points: list[tuple[int, object, object]] = []
    for i, row in enumerate(rows, start=1):
        d, v = row[date_pos], row[measure_pos]
        if d is None or v is None:
            continue
        points.append((i, d, v))
    if len(points) < 2:
        return []

    points.sort(key=lambda p: p[1])
    (prev_i, prev_d, prev_v), (last_i, last_d, last_v) = points[-2], points[-1]
    if prev_v == 0:
        return []

    pct = ((last_v - prev_v) / prev_v) * 100.0
    direction = "increased" if last_v >= prev_v else "decreased"
    evidence_cols = _evidence_columns((date_pos, date_name), (measure_pos, measure_name))
    return [AnswerInsight(
        title="Period-over-period change",
        finding=(
            f"{measure_word.capitalize()} {direction} {abs(pct):.1f}% from {_fmt_date(prev_d)} "
            f"({_fmt_value(prev_v)}) to {_fmt_date(last_d)} ({_fmt_value(last_v)})."
        ),
        evidence_row_numbers=(prev_i, last_i),
        evidence_columns=evidence_cols, provenance=_PROVENANCE,
    )]


def build_deterministic_insights(execution_result, *, max_insights: int = _MAX_INSIGHTS) -> tuple:
    """Returns a bounded tuple of grounded AnswerInsight records, or ()
    whenever any guard above applies or the result's column shape is not
    one this module understands. Never raises -- a defensive catch-all
    returns () rather than risk surfacing an ungrounded or malformed
    insight (this module's own analysis must never be able to fail a
    request that otherwise succeeded)."""
    try:
        if execution_result is None or execution_result.row_count == 0 or execution_result.truncated:
            return ()
        if execution_result.row_count == 1:
            return ()
        columns = list(execution_result.columns)
        rows = execution_result.rows
        if len(columns) < 2:
            return ()

        # A technical surrogate-key column (TrackID, StudentID, ...) sitting
        # alongside a genuine category+measure pair must not block shape
        # detection just because it is ALSO numeric -- excluded from shape
        # matching entirely (never rendered as the label or the measure),
        # not merely deprioritized.
        usable_positions = [i for i in range(len(columns)) if not _looks_like_surrogate_id_column(columns[i])]
        if not usable_positions:
            return ()

        combo = _detect_year_month_columns(columns, rows, usable_positions)
        if combo is not None:
            columns, rows = _synthesize_period_column(columns, rows, usable_positions, combo)
            usable_positions = list(range(len(columns)))

        if len(usable_positions) != 2:
            return ()

        kinds = {
            i: _column_kind([row[i] for row in rows], columns[i])
            for i in usable_positions
        }
        numeric_positions = [i for i in usable_positions if kinds[i] == "numeric"]
        date_positions = [i for i in usable_positions if kinds[i] == "date"]
        other_positions = [i for i in usable_positions if kinds[i] == "other"]

        insights: list[AnswerInsight] = []
        if len(numeric_positions) == 1 and len(date_positions) == 1:
            insights = _time_series_insight(columns, rows, date_positions[0], numeric_positions[0])
        elif len(numeric_positions) == 1 and len(other_positions) == 1:
            insights = _category_measure_insights(columns, rows, other_positions[0], numeric_positions[0])

        return tuple(insights[:max_insights])
    except Exception:  # noqa: BLE001 -- pure analysis must never break a successful answer
        return ()
