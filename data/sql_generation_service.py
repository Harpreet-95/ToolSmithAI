"""
Safe SQL Generation Engine — Program 3 Phase 5.

Converts a validated sql_plan (output of sql_planning_service.build_sql_plan)
into a safe, parameterized, read-only SQL string.

NO execution. NO source database connection. NO LLM. NO query re-planning.
Only sql_plan["validation"]["valid"] == True plans are accepted — any hard
validation failure from Phase 4 (untrusted join, unconfirmed PII, ambiguity,
injection-shaped filter, empty SELECT) propagates here as an outright refusal.

This module has no DB reads of its own. Every identifier in the output SQL
(table_fqn, column_name, alias) originates from validated plan dicts, never
from raw user strings. Filter values are always parameterized (never inlined
into the SQL string), so no user-supplied value ever appears literally in
the generated SQL regardless of its content.
"""
import logging
import re

logger = logging.getLogger(__name__)

# Defense-in-depth: verify the assembled SQL string begins with SELECT, not a
# write keyword. The plan's own validation already blocks writes — this is an
# independent check on the assembled output string, not a substitute for it.
_WRITE_STATEMENT_PATTERN = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC(?:UTE)?)\b",
    re.IGNORECASE,
)

_ALLOWED_JOIN_TYPES = frozenset({"INNER", "LEFT", "RIGHT", "FULL"})
_DEFAULT_JOIN_TYPE = "INNER"


# ---------------------------------------------------------------------------
# ANSI SQL identifier quoting
# ---------------------------------------------------------------------------

def _q(identifier: str) -> str:
    """Double-quote one identifier token, escaping embedded double-quotes."""
    return '"' + identifier.replace('"', '""') + '"'


def _qfqn(fqn: str) -> str:
    """Quote each dot-separated segment of a fully-qualified name independently.

    "dbo.orders" -> '"dbo"."orders"'
    "orders"     -> '"orders"'
    """
    return ".".join(_q(part) for part in fqn.split(".") if part)


def _qcol(table_fqn: str, column_name: str) -> str:
    """Fully-qualified, double-quoted column reference."""
    return f"{_qfqn(table_fqn)}.{_q(column_name)}"


# ---------------------------------------------------------------------------
# Clause builders — each consumes the relevant sub-list from sql_plan
# ---------------------------------------------------------------------------

def _build_select_clause(select: list[dict]) -> str:
    parts: list[str] = []
    for row in select:
        col_ref = _qcol(row["table_fqn"], row["column_name"])
        agg = row.get("aggregation")
        expr = f"{agg}({col_ref})" if agg else col_ref
        alias = _q(row.get("alias") or row["column_name"])
        parts.append(f"{expr} AS {alias}")
    return ", ".join(parts)


def _build_from_clause(from_entry: dict | None) -> str:
    if not from_entry or not from_entry.get("table_fqn"):
        return ""
    table = _qfqn(from_entry["table_fqn"])
    alias = from_entry.get("alias")
    return f"{table} AS {_q(alias)}" if alias else table


def _build_join_clauses(joins: list[dict]) -> list[str]:
    clauses: list[str] = []
    for j in joins:
        jtype = (j.get("join_type") or _DEFAULT_JOIN_TYPE).upper()
        if jtype not in _ALLOWED_JOIN_TYPES:
            jtype = _DEFAULT_JOIN_TYPE
        right_table = _qfqn(j["right_table"])
        left_col    = _qcol(j["left_table"],  j["left_column"])
        right_col   = _qcol(j["right_table"], j["right_column"])
        clauses.append(f"{jtype} JOIN {right_table} ON {left_col} = {right_col}")
    return clauses


def _build_where_clause(where: list[dict]) -> tuple[str, list]:
    """Return (predicate_string, ordered_parameter_values).

    All filter values become ? placeholders — never inlined into the SQL string.
    IN: col IN (?, ?, ?)   | BETWEEN: col BETWEEN ? AND ?  | else: col OP ?
    """
    conditions: list[str] = []
    params: list = []
    for w in where:
        col   = _qcol(w["table_fqn"], w["column_name"])
        op    = w.get("operator", "=")
        value = w.get("value")

        if op == "IN":
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            placeholders = ", ".join(["?"] * len(values))
            conditions.append(f"{col} IN ({placeholders})")
            params.extend(values)
        elif op == "BETWEEN":
            pair = list(value) if isinstance(value, (list, tuple)) and len(value) == 2 else [value, value]
            conditions.append(f"{col} BETWEEN ? AND ?")
            params.extend(pair)
        else:
            conditions.append(f"{col} {op} ?")
            params.append(value)

    return " AND ".join(conditions), params


def _build_group_by_clause(group_by: list[dict]) -> str:
    return ", ".join(_qcol(g["table_fqn"], g["column_name"]) for g in group_by)


# ---------------------------------------------------------------------------
# Refusal helper — consistent shape for all generation failures
# ---------------------------------------------------------------------------

def _refuse(reasons: list[str], warnings: list[dict]) -> dict:
    return {
        "sql":        None,
        "parameters": {"values": [], "placeholder": "?", "count": 0},
        "dialect":    "sqlite",
        "safety":     {
            "read_only": True, "parameterized": True,
            "validated": False, "select_only": False,
        },
        "warnings":    warnings,
        "explanation": [f"Generation refused: {r}" for r in reasons],
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_sql(source_id: int, user_id: str, sql_plan: dict) -> dict:
    """
    Step 2. Convert a validated sql_plan into a safe, parameterized,
    read-only SQL string.

    sql_plan must be the direct output of build_sql_plan() with
    validation.valid == True. Any hard block from Phase 4 (untrusted join,
    unconfirmed PII, empty SELECT, injection-shaped filter, ambiguity) is
    propagated here as an outright refusal — this layer does not relax them.

    source_id and user_id are accepted for logging/traceability only.
    This function makes no DB reads and opens no connections.
    """
    if not sql_plan:
        return _refuse(["No SQL plan was provided."], [])

    warnings   = list(sql_plan.get("warnings") or [])
    validation = sql_plan.get("validation") or {}

    # Step 5 — reject if Phase 4 validation did not pass
    if not validation.get("valid"):
        blocking = validation.get("blocking_reasons") or ["SQL plan validation failed."]
        return _refuse(blocking, warnings)

    select   = sql_plan.get("select") or []
    from_    = sql_plan.get("from")
    joins    = sql_plan.get("joins") or []
    where    = sql_plan.get("where") or []
    group_by = sql_plan.get("group_by") or []
    row_limit = (sql_plan.get("limits") or {}).get("row_limit")

    if not select:
        return _refuse(
            ["SELECT list is empty — refusing to generate a SELECT * equivalent."],
            warnings,
        )

    try:
        select_clause            = _build_select_clause(select)
        from_clause              = _build_from_clause(from_)
        join_clauses             = _build_join_clauses(joins)
        where_clause, params     = _build_where_clause(where)
        group_by_clause          = _build_group_by_clause(group_by)
    except Exception:
        logger.exception(
            "generate_sql: clause building failed [source_id=%s user_id=%s]",
            source_id, user_id,
        )
        return _refuse(["An internal error occurred while building SQL clauses."], warnings)

    lines: list[str] = [f"SELECT {select_clause}"]
    if from_clause:
        lines.append(f"FROM {from_clause}")
    lines.extend(join_clauses)
    if where_clause:
        lines.append(f"WHERE {where_clause}")
    if group_by_clause:
        lines.append(f"GROUP BY {group_by_clause}")
    if row_limit:
        lines.append(f"LIMIT {int(row_limit)}")

    sql = "\n".join(lines)

    # Defense-in-depth: assembled SQL must not start with a write statement
    if _WRITE_STATEMENT_PATTERN.match(sql):
        logger.error(
            "generate_sql: assembled SQL begins with a write statement [source_id=%s] — BLOCKED",
            source_id,
        )
        return _refuse(
            ["Assembled SQL did not pass the read-only assertion — generation blocked."],
            warnings,
        )

    explanation: list[str] = [
        f"Generated a read-only SELECT statement with {len(select)} column(s).",
    ]
    if joins:
        explanation.append(
            f"Joins {len(joins)} table relationship(s) via trusted (AUTO/APPROVED) paths."
        )
    if where:
        explanation.append(
            f"Applies {len(where)} filter condition(s) using {len(params)} parameterized "
            "value(s) — no filter values appear as literals in the SQL string."
        )
    if group_by:
        explanation.append(f"Groups by {len(group_by)} dimension column(s).")
    if row_limit:
        explanation.append(f"Row result capped at {row_limit}.")

    return {
        "sql":        sql,
        "parameters": {"values": params, "placeholder": "?", "count": len(params)},
        "dialect":    "sqlite",
        "safety":     {
            "read_only": True, "parameterized": True,
            "validated": True, "select_only": True,
        },
        "warnings":    warnings,
        "explanation": explanation,
    }
