"""
Safe SQL Generation Engine — Program 3 Phase 5 / Phase 5.5.

Converts a validated sql_plan (output of sql_planning_service.build_sql_plan)
into a safe, parameterized, read-only SQL string.

NO execution. NO source database connection for query purposes. NO LLM.
NO query re-planning.

Only sql_plan["validation"]["valid"] == True plans are accepted — any hard
validation failure from Phase 4 (untrusted join, unconfirmed PII, ambiguity,
injection-shaped filter, empty SELECT) propagates here as an outright refusal.

Phase 5.5: All dialect-specific SQL fragments (identifier quoting, parameter
placeholders, row-limit syntax) are routed through DialectAdapter from
data.sql_dialects. No SQL dialect string is hardcoded in this module.

generate_sql defaults to 'sqlite' dialect so that existing unit tests remain
stable without needing a live DB fixture. Callers that know the target
database type (e.g. the Phase 6 execution service) should pass
dialect=detect_dialect(source_id) explicitly.  detect_dialect() is exported
from this module for that purpose.
"""
import logging
import re

from data.sql_dialects import DialectAdapter, get_adapter, source_type_to_dialect

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
# Dialect detection — exported for Phase 6 execution service
# ---------------------------------------------------------------------------

def detect_dialect(source_id: int) -> str:
    """Return the SQL dialect for source_id by reading data_source_connections.

    Falls back to 'sqlite' on ANY failure (missing table, unknown id, DB
    unavailable) so that the caller always receives a valid dialect string.

    Phase 6 usage:
        result = generate_sql(sid, uid, plan, dialect=detect_dialect(sid))
    """
    try:
        from data import db as _db  # late import avoids circular-import issues
        conn = _db.get_connection()
        try:
            row = conn.execute(
                "SELECT source_type FROM data_source_connections WHERE id = ?",
                (source_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return "sqlite"
        return source_type_to_dialect(row["source_type"])
    except Exception:
        return "sqlite"


# ---------------------------------------------------------------------------
# Identifier helpers — delegate to DialectAdapter for all quoting
# ---------------------------------------------------------------------------

def _q(identifier: str, adapter: DialectAdapter) -> str:
    return adapter.quote_identifier(identifier)


def _qfqn(fqn: str, adapter: DialectAdapter) -> str:
    """Quote each dot-separated segment of a fully-qualified name independently."""
    parts = [p for p in fqn.split(".") if p]
    return adapter.qualified_name(*parts)


def _qcol(table_fqn: str, column_name: str, adapter: DialectAdapter) -> str:
    return f"{_qfqn(table_fqn, adapter)}.{_q(column_name, adapter)}"


# ---------------------------------------------------------------------------
# Clause builders — each consumes the relevant sub-list from sql_plan
# ---------------------------------------------------------------------------

def _build_select_clause(select: list[dict], adapter: DialectAdapter) -> str:
    parts: list[str] = []
    for row in select:
        agg = row.get("aggregation")
        column_name = row.get("column_name")

        if column_name is None:
            # Bare row-count ("How many clients?") — sql_planning_service
            # only ever produces this shape when aggregation == "COUNT".
            expr = "COUNT(*)"
        else:
            col_ref = _qcol(row["table_fqn"], column_name, adapter)
            if row.get("time_grain"):
                # Calendar-grain dimension ("each year" -> YEAR(StartDate)) —
                # wraps the raw column reference before any aggregation
                # function is applied around it.
                col_ref = adapter.date_part_expr(row["time_grain"], col_ref)
            if agg and row.get("distinct"):
                expr = f"{agg}(DISTINCT {col_ref})"
            elif agg:
                expr = f"{agg}({col_ref})"
            else:
                expr = col_ref

        alias = _q(row.get("alias") or column_name or "row_count", adapter)
        parts.append(f"{expr} AS {alias}")
    return ", ".join(parts)


def _build_order_by_clause(order_by: list[dict], adapter: DialectAdapter) -> str:
    parts: list[str] = []
    for o in order_by:
        direction = "DESC" if (o.get("direction") or "DESC").upper() == "DESC" else "ASC"
        # Milestone Phase 2 — Analytics Intent Layer. sql_planning_service's
        # _build_order_by emits an alias-only row (no table_fqn/column_name)
        # when ordering by an aggregated SELECT expression (e.g. ranking by
        # COUNT(...) per group) — reference the SELECT alias bare, since
        # ORDER BY evaluates after the SELECT list in every dialect here.
        if o.get("alias") and not o.get("table_fqn"):
            parts.append(f"{_q(o['alias'], adapter)} {direction}")
        else:
            parts.append(f"{_qcol(o['table_fqn'], o['column_name'], adapter)} {direction}")
    return ", ".join(parts)


def _build_from_clause(from_entry: dict | None, adapter: DialectAdapter) -> str:
    """
    No aliasing: every other clause builder (_qcol) always qualifies columns
    by the table's real fully-qualified name, never by from_entry["alias"] —
    so rendering `AS alias` here produced a correlation name that was never
    actually used anywhere else in the assembled SQL, and once a table is
    aliased, referencing its original name elsewhere in the same query is
    invalid in every SQL dialect this module targets. Dropping the alias
    keeps identifier usage consistent (real table names everywhere) without
    having to rewire every clause builder to alias-qualify instead.
    """
    if not from_entry or not from_entry.get("table_fqn"):
        return ""
    return _qfqn(from_entry["table_fqn"], adapter)


def _build_join_clauses(
    from_table_fqn: str | None, joins: list[dict], adapter: DialectAdapter,
) -> list[str]:
    """
    Render join_plan edges (sql_planning_service._build_joins, itself a
    verbatim per-edge copy of query_plan["join_plan"]["steps"]) as a valid
    join tree — never changes which edges exist, only how they're rendered.

    Each edge is already oriented in true FK direction (left_table
    references right_table) by recommend_best_join_path/_normalize_path_edge
    — NOT necessarily "the side already in the query comes first". A fan-in
    join_plan (two different tables independently joining to a shared
    parent — e.g. ADF_Enrollment_Tracking -> ADF_Path <- ADF_Course) has a
    second edge whose LEFT side (ADF_Course) is the new table and RIGHT side
    (ADF_Path) is already joined, the opposite of the first edge. Always
    joining `right_table` (the old behavior) re-joins ADF_Path a second
    time, unaliased (invalid/ambiguous), while ADF_Course — the table that
    edge actually needed to add — never gets a FROM/JOIN clause at all.

    Tracks the set of physical tables already present in the accumulating
    FROM/JOIN graph (seeded with the FROM table) and, per edge, joins
    whichever side isn't there yet, keeping the ON clause's left/right sides
    exactly as declared regardless of which one is the new table.
    """
    seen: set[str] = {from_table_fqn} if from_table_fqn else set()
    clauses: list[str] = []
    for j in joins:
        jtype = (j.get("join_type") or _DEFAULT_JOIN_TYPE).upper()
        if jtype not in _ALLOWED_JOIN_TYPES:
            jtype = _DEFAULT_JOIN_TYPE
        left_table, right_table = j["left_table"], j["right_table"]
        left_col  = _qcol(left_table,  j["left_column"],  adapter)
        right_col = _qcol(right_table, j["right_column"], adapter)

        left_seen, right_seen = left_table in seen, right_table in seen
        if left_seen and right_seen:
            # Both sides already joined: not a self-join request (join_plan
            # never asks for one — every currently-producible plan is a
            # tree walked from a single primary_table), so this edge is a
            # structurally redundant duplicate. Skip rather than re-join a
            # physical table a second time.
            continue
        if right_seen and not left_seen:
            new_table = left_table
        elif left_seen and not right_seen:
            new_table = right_table
        else:
            # Neither side seen yet: unreachable given how join_plan is
            # built today (every path starts at the FROM table, so an
            # edge's first occurrence always has one known side) — kept as
            # a defensive fallback, matching the pre-fix behavior, rather
            # than silently dropping an edge this module can't yet place.
            new_table = right_table
            seen.add(left_table)

        seen.add(new_table)
        clauses.append(f"{jtype} JOIN {_qfqn(new_table, adapter)} ON {left_col} = {right_col}")
    return clauses


def _build_where_clause(where: list[dict], adapter: DialectAdapter) -> tuple[str, list]:
    """Return (predicate_string, ordered_parameter_values).

    All filter values become dialect-correct placeholders — never inlined into
    the SQL string.  IN: col IN (ph, ph)  | BETWEEN: col BETWEEN ph AND ph
    | else: col OP ph
    """
    ph = adapter.param_placeholder()
    conditions: list[str] = []
    params: list = []
    for w in where:
        col   = _qcol(w["table_fqn"], w["column_name"], adapter)
        op    = w.get("operator", "=")
        value = w.get("value")

        if op == "IN":
            values = list(value) if isinstance(value, (list, tuple)) else [value]
            placeholders = adapter.placeholder_list(len(values))
            conditions.append(f"{col} IN ({placeholders})")
            params.extend(values)
        elif op == "BETWEEN":
            pair = list(value) if isinstance(value, (list, tuple)) and len(value) == 2 else [value, value]
            conditions.append(f"{col} BETWEEN {ph} AND {ph}")
            params.extend(pair)
        elif op in ("IS NOT NULL", "IS NULL"):
            # No value/placeholder — a null-check filter (Day 2C follow-up,
            # "on file"/"on record" idiom -> real filter) carries no operand.
            conditions.append(f"{col} {op}")
        else:
            conditions.append(f"{col} {op} {ph}")
            params.append(value)

    return " AND ".join(conditions), params


def _build_group_by_clause(group_by: list[dict], adapter: DialectAdapter) -> str:
    parts = []
    for g in group_by:
        col_ref = _qcol(g["table_fqn"], g["column_name"], adapter)
        if g.get("time_grain"):
            col_ref = adapter.date_part_expr(g["time_grain"], col_ref)
        parts.append(col_ref)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Refusal helper — consistent shape for all generation failures
# ---------------------------------------------------------------------------

def _refuse(reasons: list[str], warnings: list[dict], dialect: str = "sqlite") -> dict:
    return {
        "sql":        None,
        "parameters": {"values": [], "placeholder": "?", "count": 0},
        "dialect":    dialect,
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

def generate_sql(
    source_id: int,
    user_id: str,
    sql_plan: dict,
    *,
    dialect: str = "sqlite",
) -> dict:
    """Convert a validated sql_plan into a safe, parameterized, read-only SQL string.

    sql_plan must be the direct output of build_sql_plan() with
    validation.valid == True. Any hard block from Phase 4 is propagated here
    as an outright refusal — this layer does not relax them.

    dialect — keyword argument selecting the SQL dialect.  Defaults to 'sqlite'
    so that existing callers and unit tests remain unchanged.  Callers that need
    dialect-specific SQL (e.g. the Phase 6 execution service) should pass
    dialect=detect_dialect(source_id).  Supported values: 'sqlite', 'mssql',
    'postgresql', 'mysql'.

    source_id and user_id are accepted for logging/traceability.
    """
    if not sql_plan:
        return _refuse(["No SQL plan was provided."], [])

    warnings   = list(sql_plan.get("warnings") or [])
    validation = sql_plan.get("validation") or {}

    if not validation.get("valid"):
        blocking = validation.get("blocking_reasons") or ["SQL plan validation failed."]
        return _refuse(blocking, warnings)

    resolved_dialect = dialect
    try:
        adapter = get_adapter(resolved_dialect)
    except ValueError:
        logger.warning(
            "generate_sql: unknown dialect %r for source_id=%s — falling back to sqlite",
            resolved_dialect, source_id,
        )
        resolved_dialect = "sqlite"
        adapter = get_adapter("sqlite")

    select    = sql_plan.get("select") or []
    from_     = sql_plan.get("from")
    joins     = sql_plan.get("joins") or []
    where     = sql_plan.get("where") or []
    group_by  = sql_plan.get("group_by") or []
    order_by  = sql_plan.get("order_by") or []
    distinct  = bool(sql_plan.get("distinct"))
    row_limit = (sql_plan.get("limits") or {}).get("row_limit")

    if not select:
        return _refuse(
            ["SELECT list is empty — refusing to generate a SELECT * equivalent."],
            warnings, resolved_dialect,
        )

    try:
        select_clause         = _build_select_clause(select, adapter)
        from_clause           = _build_from_clause(from_, adapter)
        join_clauses          = _build_join_clauses(
            from_.get("table_fqn") if from_ else None, joins, adapter,
        )
        where_clause, params  = _build_where_clause(where, adapter)
        group_by_clause       = _build_group_by_clause(group_by, adapter)
        order_by_clause       = _build_order_by_clause(order_by, adapter)
    except Exception:
        logger.exception(
            "generate_sql: clause building failed [source_id=%s user_id=%s]",
            source_id, user_id,
        )
        return _refuse(
            ["An internal error occurred while building SQL clauses."],
            warnings, resolved_dialect,
        )

    limit_prefix = adapter.row_limit_prefix(int(row_limit)) if row_limit else ""
    limit_suffix = adapter.row_limit_suffix(int(row_limit)) if row_limit else ""
    distinct_prefix = "DISTINCT " if distinct else ""

    lines: list[str] = [f"SELECT {limit_prefix}{distinct_prefix}{select_clause}"]
    if from_clause:
        lines.append(f"FROM {from_clause}")
    lines.extend(join_clauses)
    if where_clause:
        lines.append(f"WHERE {where_clause}")
    if group_by_clause:
        lines.append(f"GROUP BY {group_by_clause}")
    if order_by_clause:
        lines.append(f"ORDER BY {order_by_clause}")
    if limit_suffix:
        lines.append(limit_suffix)

    sql = "\n".join(lines)

    # Defense-in-depth: assembled SQL must not start with a write statement
    if _WRITE_STATEMENT_PATTERN.match(sql):
        logger.error(
            "generate_sql: assembled SQL begins with a write statement "
            "[source_id=%s] — BLOCKED", source_id,
        )
        return _refuse(
            ["Assembled SQL did not pass the read-only assertion — generation blocked."],
            warnings, resolved_dialect,
        )

    explanation: list[str] = [
        f"Generated a read-only SELECT statement with {len(select)} column(s) "
        f"using {resolved_dialect} dialect.",
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
    if order_by:
        explanation.append(f"Ordered by {len(order_by)} column(s) ({resolved_dialect} style).")
    if distinct:
        explanation.append("Duplicate rows are removed (DISTINCT).")
    if row_limit:
        explanation.append(f"Row result capped at {row_limit} ({resolved_dialect} style).")

    return {
        "sql":        sql,
        "parameters": {
            "values":      params,
            "placeholder": adapter.param_placeholder(),
            "count":       len(params),
        },
        "dialect":    resolved_dialect,
        "safety":     {
            "read_only": True, "parameterized": True,
            "validated": True, "select_only": True,
        },
        "warnings":    warnings,
        "explanation": explanation,
    }
