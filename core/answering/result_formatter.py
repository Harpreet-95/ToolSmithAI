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
            f"No matching {entity_label} were found for the selected filters."
            if applied_filters else f"No matching {entity_label} were found."
        )
        return {
            **common,
            "answer": answer, "summary": f"No {entity_label} found.",
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
        answer = f"There are {formatted} {filt_adj}{qualifier}{entity_label}."
        return {
            **common,
            "answer": answer, "summary": f"{formatted} {entity_label}.",
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
        answer = f"The {verb} {measure_label} is {formatted}."
        return {
            **common,
            "answer": answer, "summary": f"{verb.capitalize()} {measure_label}: {formatted}.",
            "actual_value": value, "result_preview": [],
            "business_entity": entity_label, "measure": measure_label, "aggregation": plan.get("aggregation"),
            "limitations": [],
        }

    if shape == "grouped":
        dim_label = _first_dimension_label(plan) or "category"
        preview = _labeled_preview(rows, plan)
        answer = f"{entity_label.capitalize()} are grouped below by {dim_label}."
        if truncated:
            answer += " Only the first rows are shown."
        return {
            **common,
            "answer": answer, "summary": f"Grouped by {dim_label}.",
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
        by_clause = f" by {measure_label}" if plan.get("aggregation") else ""
        answer = f"The {direction_word} {limit} {entity_label}{by_clause} are shown below."
        if truncated:
            answer += " Results were truncated by the configured row limit."
        return {
            **common,
            "answer": answer, "summary": f"{direction_word.capitalize()} {limit} {entity_label}.",
            "actual_value": None, "result_preview": preview,
            "business_entity": entity_label,
            "measure": measure_label if plan.get("aggregation") else None,
            "aggregation": plan.get("aggregation"),
            "limitations": ["Results were truncated."] if truncated else [],
        }

    # tabular
    preview = _labeled_preview(rows, plan)
    answer = f"{row_count} matching {entity_label} record(s) are shown below."
    if truncated:
        answer += " Results were truncated by the configured row limit."
    return {
        **common,
        "answer": answer, "summary": f"{row_count} {entity_label} record(s).",
        "actual_value": None, "result_preview": preview,
        "business_entity": entity_label, "measure": None, "aggregation": None,
        "limitations": ["Results were truncated."] if truncated else [],
    }
