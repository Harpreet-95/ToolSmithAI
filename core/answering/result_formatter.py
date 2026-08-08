from __future__ import annotations

import re

_PREVIEW_LIMIT = 50


def _humanize(name: str) -> str:
    words = re.split(r"[_\s]+", name or "")
    return " ".join(w.capitalize() for w in words if w) or (name or "")


def format_value(value) -> str:
    """Type-driven formatting only — never assumes a decimal is currency, per
    the milestone brief's formatter-safety rule. No governed currency/unit
    metadata exists anywhere in the schema to justify a currency symbol."""
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if value.is_integer():
            return f"{int(value):,}"
        return f"{value:,.2f}"
    return str(value)


def _first_value(row: dict | None):
    if not row:
        return None
    return next(iter(row.values()), None)


def classify_result_shape(data: dict) -> str:
    """Deterministic classification off business_plan + result shape — never
    invents meaning; falls back to a generic tabular shape when business_plan
    is absent (the raw-SQL trusted-caller bypass has no query/sql plan)."""
    plan = data.get("business_plan")
    if plan is None:
        return "tabular_fallback"

    row_count = data.get("row_count") or 0
    if row_count == 0:
        return "empty"

    rows = data.get("rows") or []
    aggregation = plan.get("aggregation")
    group_by = plan.get("group_by") or []
    order_intent = plan.get("order_intent") or {}
    ranked = bool(order_intent.get("limit"))

    is_scalar = row_count == 1 and not group_by and aggregation and not ranked
    if is_scalar:
        value = _first_value(rows[0] if rows else None)
        if value is None:
            return "null_scalar"
        if aggregation == "COUNT":
            return "scalar_count_distinct" if plan.get("distinct") else "scalar_count"
        if aggregation == "SUM":
            return "scalar_sum"
        if aggregation == "AVG":
            return "scalar_avg"
        if aggregation in ("MIN", "MAX"):
            return "scalar_minmax"

    if ranked:
        return "ranked"
    if group_by:
        return "grouped"
    return "tabular"


def _build_applied_filters(plan: dict) -> list[dict]:
    filters = []
    for w in plan.get("where") or []:
        column = w.get("column_name") or ""
        filters.append({
            "label": _humanize(column) or column,
            "operator": w.get("operator"),
            "value": w.get("value"),
        })
    return filters


def _build_source_columns(plan: dict) -> list[str]:
    cols = {
        f"{row['table_fqn']}.{row['column_name']}"
        for row in (plan.get("select") or [])
        if row.get("column_name")
    }
    return sorted(cols)


def _alias_label_map(plan: dict) -> dict[str, str]:
    dims = plan.get("dimension_labels") or {}
    mapping: dict[str, str] = {}
    for row in plan.get("select") or []:
        alias = row.get("alias")
        if alias is None:
            continue
        column = row.get("column_name")
        if column and column in dims:
            mapping[alias] = dims[column]
        elif row.get("aggregation"):
            mapping[alias] = plan.get("measure_label") or plan.get("entity_label") or _humanize(alias)
        else:
            mapping[alias] = _humanize(column or alias)
    return mapping


def _labeled_preview(rows: list[dict], plan: dict) -> list[dict]:
    mapping = _alias_label_map(plan)
    return [
        {mapping.get(k, _humanize(k)): v for k, v in row.items()}
        for row in rows[:_PREVIEW_LIMIT]
    ]


def _first_dimension_label(plan: dict) -> str | None:
    labels = plan.get("dimension_labels") or {}
    for g in plan.get("group_by") or []:
        column = g.get("column_name")
        if column and column in labels:
            return labels[column]
    group_by = plan.get("group_by") or []
    if group_by:
        return _humanize(group_by[0].get("column_name") or "")
    return None


_CHART_ELIGIBLE_SHAPES = {"grouped", "ranked"}
_MAX_CHART_CATEGORIES = 24  # beyond this a chart stops being legible; the table remains the right view
_DONUT_MAX_CATEGORIES = 6


def _build_chart_spec(shape: str, plan: dict, rows: list[dict]) -> dict | None:
    """Day 4, Capability 3 — Automatic Charts. Deterministic chart-type
    selection derived only from the already-validated business_plan/rows —
    never issues new SQL, never invents values or categories, never picks a
    chart type by guessing at label text (contrast
    ChartSection.jsx's own recommendChartType, which infers from label
    strings and would misclassify a bare year like "2023" as a numeric
    histogram bin rather than a time series — this uses the structured
    time_grain signal data.sql_planning_service._build_group_by already
    attaches instead).

    Returns None whenever the result shape doesn't support a meaningful
    chart: every scalar/null/empty/tabular shape (no chart for a single
    number), a multi-dimension group-by (no clean single label/series
    mapping to invent), too many categories to read, or any structural
    mismatch between the resolved dimension/measure aliases and the actual
    row keys. A None here always means "show the answer/table normally,"
    never an error.
    """
    if shape not in _CHART_ELIGIBLE_SHAPES:
        return None
    group_by = plan.get("group_by") or []
    if len(group_by) != 1:
        return None
    dimension_alias = group_by[0].get("alias") or group_by[0].get("column_name")
    if not dimension_alias:
        return None

    select = plan.get("select") or []
    measure_row = next((r for r in select if r.get("aggregation")), None)
    measure_alias = measure_row.get("alias") if measure_row else None
    if not measure_alias:
        return None

    if not rows or len(rows) > _MAX_CHART_CATEGORIES:
        return None

    labels: list[str] = []
    values: list[float] = []
    for row in rows:
        if dimension_alias not in row or measure_alias not in row:
            return None
        value = row[measure_alias]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        labels.append(str(row[dimension_alias]))
        values.append(value)

    if shape == "ranked":
        chart_type = "bar_horizontal"
    elif group_by[0].get("time_grain"):
        chart_type = "line"
    elif plan.get("aggregation") == "COUNT" and 2 <= len(labels) <= _DONUT_MAX_CATEGORIES:
        chart_type = "donut"
    else:
        chart_type = "bar"

    measure_label = plan.get("measure_label") or plan.get("entity_label") or "value"
    return {"chart_type": chart_type, "labels": labels, "series": [{"name": measure_label, "data": values}]}


# ---------------------------------------------------------------------------
# Day 4, Capability 4 — Natural-Language Answer Quality. Deterministic
# templates only (no LLM call) — every helper below reads exclusively from
# business_plan/rows/insight, the same already-verified evidence
# build_business_answer already had; nothing here re-queries, re-plans, or
# invents a value/label/cause not already present upstream.
# ---------------------------------------------------------------------------

_GRAIN_ADVERB = {"year": "yearly", "quarter": "quarterly", "month": "monthly", "week": "weekly", "day": "daily"}
_TECHNICAL_PREFIX_RE = re.compile(r"^(?:[a-z]{2,5}_|vw_|dbo\.)", re.IGNORECASE)


def _join_naturally(parts: list[str]) -> str:
    """'a' | 'a and b' | 'a, b, and c' — an Oxford comma only kicks in at
    3+ items; two items read as a plain "a and b", never "a, and b"."""
    if len(parts) <= 1:
        return parts[0] if parts else ""
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _naive_pluralize(word: str) -> str:
    """Grammatical normalization only — never a meaning change. Handles the
    common English cases well enough for a business noun derived from a
    table/column name; irregular plurals (e.g. "company" -> "companies" is
    handled, "person" -> "persons" is not) are an accepted V1 gap rather
    than a risk, since the word is never wrong, at worst slightly informal."""
    if not word or not word.isalpha():
        return word
    lower = word.lower()
    if lower.endswith("s"):
        return word
    if lower.endswith(("ch", "sh", "x", "z")):
        return word + "es"
    if lower.endswith("y") and len(word) > 1 and word[-2].lower() not in "aeiou":
        return word[:-1] + "ies"
    return word + "s"


def _business_noun(plan: dict, entity_label: str) -> str:
    """The plural, business-facing noun for this entity, reusing the SAME
    vocabulary evidence that already surfaces as a citation ("Resolved using
    database vocabulary: 'students' -> 'student'", citation_builder.py
    ._cite_generated_vocabulary/._cite_remembered_terminology) — never
    invents a friendlier name; it is exactly the term the user already
    typed, already verified by the vocabulary/dictionary resolution system.

    Falls back to a grammatically normalized entity_label when no
    vocabulary substitution was recorded for THIS query (some resolution
    paths — e.g. a grouped/dimension query — don't always attach that
    evidence even when a scalar query against the same table did): strips a
    leading technical prefix (e.g. "ADF_", "vw_", "dbo.") and pluralizes the
    remaining word. This is cosmetic normalization of the SAME name already
    given, never an invented alternate name — table_fqn/entity_label
    themselves are untouched everywhere else (citations, technical
    details)."""
    for item in (plan.get("remembered_terminology") or []):
        term = item.get("original_term")
        if term:
            return term
    for item in (plan.get("generated_vocabulary_evidence") or []):
        term = item.get("original_term")
        if term:
            return term
    stripped = _TECHNICAL_PREFIX_RE.sub("", entity_label or "")
    humanized = _humanize(stripped or entity_label)
    return " ".join(_naive_pluralize(w) if i == len(humanized.split()) - 1 else w
                     for i, w in enumerate(humanized.split())).lower()


def _insight_clause(insight: "dict | None") -> str:
    """Folds an already-computed data.insight_service period-comparison into
    one trailing clause — reuses percent_change/direction verbatim, never
    re-derives them. Empty string when insight is absent/incomplete, so
    callers can unconditionally append it."""
    if not insight:
        return ""
    pct = insight.get("percent_change")
    direction = insight.get("direction")
    if pct is None or direction is None:
        return ""
    if direction == "flat":
        return "That's flat compared to the previous period."
    verb = "up" if direction == "up" else "down"
    return f"That's {verb} {abs(pct):g}% from the previous period."


def _extract_series_for_summary(plan: dict, rows: list[dict]) -> "tuple[list[str], list[float]] | None":
    """Independent of _build_chart_spec (Capability 3, frozen) — duplicates
    its small alias-resolution logic rather than sharing code, so nothing
    here can ever change chart-selection behavior. Returns (labels, values)
    in the SAME order as `rows` (chronological for a time grouping already
    ordered that way upstream, ranked order for a ranked result) or None
    when the shape can't cleanly support a single dimension/measure
    extraction — every caller below has a safe, generic sentence fallback
    for None."""
    group_by = plan.get("group_by") or []
    if group_by:
        if len(group_by) != 1:
            return None
        dimension_alias = group_by[0].get("alias") or group_by[0].get("column_name")
    else:
        dimension_alias = next(
            (r.get("alias") for r in (plan.get("select") or []) if not r.get("aggregation")), None,
        )
    if not dimension_alias:
        return None

    measure_row = next((r for r in (plan.get("select") or []) if r.get("aggregation")), None)
    measure_alias = measure_row.get("alias") if measure_row else None
    if not measure_alias or not rows:
        return None

    labels: list[str] = []
    values: list[float] = []
    for row in rows:
        if dimension_alias not in row or measure_alias not in row:
            return None
        value = row[measure_alias]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        raw_label = row[dimension_alias]
        # A genuinely blank category (empty string on file, distinct from a
        # NULL — both occur in real data) is still real, verified data and
        # must be named, not dropped — but an empty string breaks sentence
        # grammar ("followed by  and NA"), so it gets a neutral placeholder
        # here, prose-only; the raw value is untouched everywhere else.
        text = str(raw_label) if raw_label is not None else "None"
        labels.append(text if text.strip() else "(unspecified)")
        values.append(value)
    return labels, values


def _grouped_answer(plan: dict, rows: list[dict], noun: str) -> "tuple[str, str]":
    """Natural-language (answer, summary) for a grouped categorical or
    time-series result — Rule set B/C."""
    dim_label = _first_dimension_label(plan) or "category"
    aggregation = plan.get("aggregation")
    measure_label = plan.get("measure_label") or plan.get("entity_label") or "value"
    group_by = plan.get("group_by") or []
    is_time_series = bool(group_by and group_by[0].get("time_grain"))

    series = _extract_series_for_summary(plan, rows)
    if series is None:
        return f"{noun.capitalize()} are grouped below by {dim_label}.", f"Grouped by {dim_label}."
    labels, values = series
    ranked_pairs = sorted(zip(labels, values), key=lambda p: p[1], reverse=True)

    if is_time_series:
        peak_label, peak_val = ranked_pairs[0]
        adverb = _GRAIN_ADVERB.get(group_by[0].get("time_grain"), dim_label.lower())
        subject = noun.capitalize() if aggregation == "COUNT" else measure_label
        trailing = f" {noun}" if aggregation == "COUNT" else ""
        answer = f"{subject} peaked in {peak_label} with {format_value(peak_val)}{trailing}."
        answer += f" The dataset shows {len(labels)} {adverb} groups from {labels[0]} through {labels[-1]}."
        return answer, f"Peak: {format_value(peak_val)} in {peak_label}."

    top_label, top_val = ranked_pairs[0]
    lede = f"Most {noun} are {top_label}" if aggregation == "COUNT" else f"{measure_label} is highest for {top_label}"
    if len(ranked_pairs) > 1 and top_val > 0 and ranked_pairs[1][1] >= top_val * 0.7:
        lede += f" or {ranked_pairs[1][0]}"
    lede += "."

    if len(ranked_pairs) <= 6:
        parts = [f"{format_value(v)} {str(lbl).lower()}" for lbl, v in ranked_pairs]
        body = f" The current breakdown is {_join_naturally(parts)}."
    else:
        body = f" The dataset shows {len(ranked_pairs)} {dim_label.lower()} groups."
    return lede + body, f"Top: {top_label} ({format_value(top_val)})."


def _ranked_answer(plan: dict, rows: list[dict], noun: str, direction_word: str, limit: int) -> "tuple[str, str]":
    """Natural-language (answer, summary) for a top-N/bottom-N ranked
    result — Rule set D."""
    aggregation = plan.get("aggregation")
    measure_label = plan.get("measure_label") or plan.get("entity_label") or "value"
    dim_label = _first_dimension_label(plan) or "category"

    series = _extract_series_for_summary(plan, rows)
    if series is None:
        by_clause = f" by {measure_label}" if aggregation else ""
        return (
            f"The {direction_word} {limit} {noun}{by_clause} are shown below.",
            f"{direction_word.capitalize()} {limit} {noun}.",
        )

    labels, values = series
    trailing = f" {noun}" if aggregation == "COUNT" else ""
    if direction_word == "top":
        lede = f"{labels[0]} leads with {format_value(values[0])}{trailing}"
    else:
        lede = f"{labels[0]} has the fewest, with {format_value(values[0])}{trailing}"
    rest = labels[1:3]
    if rest:
        lede += f", followed by {_join_naturally(rest)}"
    lede += "."
    return lede, f"{direction_word.capitalize()} {limit} {dim_label.lower()}."


def _tabular_answer(plan: dict, row_count: int, noun: str) -> "tuple[str, str]":
    """Natural-language (answer, summary) for a raw/joined row list — Rule
    set E, "relationship-list result." Names the entities being shown using
    already-computed business labels — never a raw table name."""
    related: list[str] = []
    seen = {noun}
    for label in (plan.get("dimension_labels") or {}).values():
        low = _humanize(label).lower()
        if low and low not in seen:
            seen.add(low)
            related.append(low)
    answer = f"{format_value(row_count)} {noun} are shown below"
    shown = related[:2]
    if shown:
        answer += f", including {' and '.join(shown)}"
    answer += "."
    return answer, f"{format_value(row_count)} {noun}."


def build_business_answer(data: dict) -> dict:
    """Deterministic, template-based business-language answer for a
    successfully executed live query. Never uses an LLM to invent meaning —
    every value/label comes straight from business_plan or the result rows
    already validated upstream."""
    shape = classify_result_shape(data)
    plan = data.get("business_plan") or {}
    row_count = data.get("row_count") or 0
    rows = data.get("rows") or []
    columns = data.get("columns") or []
    truncated = bool(data.get("truncated"))

    entity_label = plan.get("entity_label") or "record(s)"
    measure_label = plan.get("measure_label") or "value"
    # Day 4, Capability 4 — the natural-language business noun used in
    # "answer"/"summary" prose below; entity_label itself is left untouched
    # everywhere else (business_entity, citations, etc. are unaffected).
    noun = _business_noun(plan, entity_label)
    applied_filters = _build_applied_filters(plan)
    date_context = plan.get("date_context")
    source_tables = plan.get("source_tables") or []
    source_columns = _build_source_columns(plan)
    truncation_notice = (
        f"Only the first {row_count} row(s) are shown; more matching rows exist."
        if truncated else None
    )

    common = {
        "applied_filters": applied_filters,
        "date_context": date_context,
        "source_tables": source_tables,
        "source_columns": source_columns,
        "truncation_notice": truncation_notice,
        "assumptions": [],
        # Day 4, Capability 2 — Business Insights. Set only by
        # core.orchestrator.agent for a time-bound single-scalar aggregate;
        # None for every other shape/path, same as every other data.get()
        # here that isn't always populated.
        "insight": data.get("insight"),
        # Day 4, Capability 3 — Automatic Charts. None whenever the shape/
        # plan/rows don't cleanly support one — see _build_chart_spec.
        "chart": _build_chart_spec(shape, plan, rows),
    }

    if shape == "tabular_fallback":
        answer = f"The live query returned {row_count} row(s) across {len(columns)} column(s)."
        if truncated:
            answer += " Results were truncated by the configured row limit."
        return {
            **common,
            "answer": answer, "summary": f"{row_count} rows returned.",
            "actual_value": None, "result_preview": rows[:_PREVIEW_LIMIT],
            "business_entity": None, "measure": None, "aggregation": None,
            "limitations": ["Results were truncated."] if truncated else [],
        }

    if shape == "empty":
        answer = (
            f"No matching {noun} were found for the selected filters."
            if applied_filters else f"No matching {noun} were found."
        )
        return {
            **common,
            "answer": answer, "summary": f"No {noun} found.",
            "actual_value": None, "result_preview": [],
            "business_entity": entity_label, "measure": None, "aggregation": plan.get("aggregation"),
            "limitations": [],
        }

    if shape == "null_scalar":
        answer = f"The query completed, but no value was available for {measure_label}."
        return {
            **common,
            "answer": answer, "summary": "No value available.",
            "actual_value": None, "result_preview": [],
            "business_entity": entity_label, "measure": measure_label, "aggregation": plan.get("aggregation"),
            "limitations": ["The aggregated value was NULL — likely no matching rows contributed to it."],
        }

    if shape in ("scalar_count", "scalar_count_distinct"):
        value = _first_value(rows[0])
        formatted = format_value(value)
        qualifier = "unique " if shape == "scalar_count_distinct" else ""
        status_label = plan.get("status_label")
        filt_adj = f"{status_label.lower()} " if status_label else ""
        if date_context:
            # Prose-only cosmetic: the raw label (e.g. "last_quarter") is
            # left untouched everywhere else (the 📅 chip, citations).
            time_phrase = (date_context.get("label") or "").replace("_", " ")
            answer = f"There were {formatted} {filt_adj}{qualifier}{noun} {time_phrase}."
        else:
            answer = f"There are {formatted} {filt_adj}{qualifier}{noun} in the database."
        insight_clause = _insight_clause(data.get("insight"))
        if insight_clause:
            answer += " " + insight_clause
        return {
            **common,
            "answer": answer, "summary": f"{formatted} {noun}.",
            "actual_value": value, "result_preview": [],
            "business_entity": entity_label, "measure": None, "aggregation": plan.get("aggregation"),
            "limitations": [],
        }

    if shape in ("scalar_sum", "scalar_avg", "scalar_minmax"):
        value = _first_value(rows[0])
        formatted = format_value(value)
        verb = {
            "scalar_sum": "total", "scalar_avg": "average",
            "scalar_minmax": "minimum" if plan.get("aggregation") == "MIN" else "maximum",
        }[shape]
        answer = f"{verb.capitalize()} {measure_label} is {formatted}."
        if shape in ("scalar_sum", "scalar_avg"):
            insight_clause = _insight_clause(data.get("insight"))
            if insight_clause:
                answer += " " + insight_clause
        return {
            **common,
            "answer": answer, "summary": f"{verb.capitalize()} {measure_label}: {formatted}.",
            "actual_value": value, "result_preview": [],
            "business_entity": entity_label, "measure": measure_label, "aggregation": plan.get("aggregation"),
            "limitations": [],
        }

    if shape == "grouped":
        preview = _labeled_preview(rows, plan)
        answer, summary = _grouped_answer(plan, rows, noun)
        if truncated:
            answer += " Only the first rows are shown."
        return {
            **common,
            "answer": answer, "summary": summary,
            "actual_value": None, "result_preview": preview,
            "business_entity": entity_label,
            "measure": measure_label if plan.get("aggregation") else None,
            "aggregation": plan.get("aggregation"),
            "limitations": ["Results were truncated."] if truncated else [],
        }

    if shape == "ranked":
        order_intent = plan.get("order_intent") or {}
        limit = order_intent.get("limit") or row_count
        direction_word = "bottom" if order_intent.get("direction") == "ASC" else "top"
        preview = _labeled_preview(rows, plan)
        answer, summary = _ranked_answer(plan, rows, noun, direction_word, limit)
        if truncated:
            answer += " Results were truncated by the configured row limit."
        return {
            **common,
            "answer": answer, "summary": summary,
            "actual_value": None, "result_preview": preview,
            "business_entity": entity_label,
            "measure": measure_label if plan.get("aggregation") else None,
            "aggregation": plan.get("aggregation"),
            "limitations": ["Results were truncated."] if truncated else [],
        }

    # tabular
    preview = _labeled_preview(rows, plan)
    answer, summary = _tabular_answer(plan, row_count, noun)
    if truncated:
        answer += " Results were truncated by the configured row limit."
    return {
        **common,
        "answer": answer, "summary": summary,
        "actual_value": None, "result_preview": preview,
        "business_entity": entity_label, "measure": None, "aggregation": None,
        "limitations": ["Results were truncated."] if truncated else [],
    }
