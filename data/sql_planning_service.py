"""
SQL Planning & Validation Engine — Program 3 Phase 4.

Transforms the structured output of
query_planning_service.plan_business_query() into a SQL-ready PLAN —
select/from/joins/where/group_by/order_by/limits — and validates it against
a fixed set of safety rules.

NO SQL string generation. NO execution. NO LLM. Does not call
find_business_assets / get_table_business_context / analyze_join_quality /
recommend_best_join_path again — query_plan already carries everything
Steps 3-6 need (it IS plan_business_query's output: measures/dimensions with
resolved table_fqn+column_name, a join_plan with per-edge join_type/
cardinality/fanout_risk/confidence already trust-filtered to AUTO/APPROVED
relationships, resolved filters, and the full candidate column set).

The new calls this module makes are business_knowledge_service.
get_column_business_context, used only for precise structured PII/approval
validation (Step 8) on the small set of columns actually selected, and
business_knowledge_service.get_table_business_context, used only for the
bare-entity-list column-selection carve-out (intent.type == "list_entities")
to choose which of a table's known columns are safe to auto-select — one
bounded call per resolved list-entity table, never per column. Everything
else is reached transitively through query_plan, exactly as
query_planning_service reached profiling_service/governance_service
transitively through get_table_business_context.
"""
import logging
import re

from data.business_knowledge_service import get_column_business_context, get_table_business_context

logger = logging.getLogger(__name__)

_ALLOWED_OPERATORS = {"=", "!=", ">", ">=", "<", "<=", "IN", "BETWEEN", "LIKE", "IS NOT NULL", "IS NULL"}

# Enterprise AI Analyst Agent — bare entity list routing (intent.type ==
# "list_entities"). A concept resolves to a whole table, not a column, so
# there is no measure/dimension column to select — cap the listed columns
# instead of an unbounded SELECT *.
_MAX_LIST_ENTITY_COLUMNS = 25

# Defense-in-depth: reject filter values shaped like raw SQL injection
# attempts, even though this layer never builds a SQL string itself — the
# guarantee should still hold for whenever a future phase parameterizes
# these values into real SQL.
_UNSAFE_VALUE_PATTERN = re.compile(
    r";|--|/\*|\*/|\b(DROP|DELETE|UPDATE|EXEC|EXECUTE|INSERT|ALTER|TRUNCATE|UNION)\b",
    re.IGNORECASE,
)


def _is_unsafe_value(value) -> bool:
    return isinstance(value, str) and bool(_UNSAFE_VALUE_PATTERN.search(value))


def _short_alias(table_fqn: str, used: set[str]) -> str:
    base = (table_fqn.split(".")[-1][:3] or "t").lower()
    alias = base
    i = 1
    while alias in used:
        i += 1
        alias = f"{base}{i}"
    used.add(alias)
    return alias


def _columns_known(table_fqn: str | None, column_name: str | None, known_columns: dict) -> bool:
    if not table_fqn or not column_name:
        return False
    return column_name in (known_columns.get(table_fqn) or [])


def _grain_alias(column_name: str, grain: str) -> str:
    """'StartDate' + 'year' -> 'start_year'. Strips a trailing "Date" suffix
    (the overwhelmingly common naming convention for the date columns this
    resolves against) so the alias reads as the grouped value, not the raw
    column; falls back to the full lowercased column name when there's no
    such suffix to strip."""
    base = re.sub(r"(?i)date$", "", column_name).strip() or column_name
    return f"{base.lower()}_{grain}"


# ---------------------------------------------------------------------------
# Step 3 — SELECT planning
# ---------------------------------------------------------------------------

def _select_entries(
    entries: list[dict], aggregation: str | None, *, distinct: bool = False,
) -> tuple[list[dict], list[str]]:
    """
    entries = query_plan["measures"] or query_plan["dimensions"]. Returns
    (select_rows, unresolved_terms) — unresolved_terms feed the Step 8
    ambiguity block; dimensions always pass aggregation=None (never
    aggregated, per Step 3).

    column_name may be None — query_planning_service._resolve_count_all
    synthesizes this for a bare row-count question ("How many clients?")
    where the term names a whole table, not a metric column. That renders
    as COUNT(*) downstream, never a fabricated column reference.

    distinct (only meaningful when aggregation is set and column_name is a
    real column) marks this row for COUNT(DISTINCT col) rendering — e.g.
    "how many unique clients". Never applied to the COUNT(*) row-count case,
    since there is no column to de-duplicate on.

    Milestone Phase 6.2: an entity-count entry (query_planning_service.
    _resolve_entity_count) may itself carry a per-entry `selected["distinct"]`
    — set when the question explicitly asked for a distinct count, or forced
    by join fan-out safety once a trusted key was found. That per-entry flag
    is honored in addition to the caller-supplied `distinct` (which reflects
    the ordinary query-level "distinct"/"unique" phrasing) — `sel.get(...)`
    is absent/False on every pre-existing measure/dimension shape, so this
    is inert for anything that isn't an entity-count entry.
    """
    rows: list[dict] = []
    unresolved: list[str] = []
    for entry in entries:
        sel = entry.get("selected")
        if sel is None:
            unresolved.append(entry["term"])
            continue
        column_name = sel.get("column_name")
        time_grain = sel.get("time_grain")
        if column_name is None:
            alias = "row_count"
        elif time_grain:
            # Calendar-grain dimension ("each year" -> YEAR(StartDate)) —
            # aliased on the grain, not the raw column, so
            # "start_year"/"start_month" reads as the grouped value it is,
            # not the raw date column.
            alias = _grain_alias(column_name, time_grain)
        else:
            alias = f"{aggregation.lower()}_{column_name}" if aggregation else column_name
        rows.append({
            "table_fqn":   sel["table_fqn"],
            "column_name": column_name,
            "alias":       alias,
            "aggregation": aggregation,
            "distinct":    bool((distinct or sel.get("distinct")) and aggregation and column_name is not None),
            "time_grain":  time_grain,
        })
    return rows, unresolved


# ---------------------------------------------------------------------------
# Step 9 — ORDER BY planning (Top N / Bottom N / Latest / Earliest)
# ---------------------------------------------------------------------------

def _build_order_by(
    order: dict | None, known_columns: dict,
    select_rows: list[dict] | None = None, has_group_by: bool = False,
) -> tuple[list[dict], str | None]:
    """
    `order` is query_planning_service's already-resolved
    {"direction", "limit", "table_fqn"?, "column_name"?} — the target column
    was resolved once in the semantic layer, exactly like measures/
    dimensions/joins. Only ever produces an entry for a column that is
    already a known, discovered column — never invents a sort column.

    Returns (order_by_rows, warning_message_or_None). A request with no
    resolvable column is NOT a blocking failure — the row cap (Step 10)
    still applies, it just isn't sorted.
    """
    if not order:
        return [], None
    table_fqn = order.get("table_fqn")
    column_name = order.get("column_name")
    if not table_fqn or not column_name or not _columns_known(table_fqn, column_name, known_columns):
        return [], "Ordering was requested but no valid column was resolved to sort by."

    # Milestone Phase 2 — Analytics Intent Layer. When the ordered column is
    # ALSO one of the query's own aggregated SELECT rows AND the query has a
    # GROUP BY (e.g. ranking by COUNT(enrollment) per course), ordering by
    # the raw, ungrouped column is invalid SQL. Reference the SELECT alias
    # already built for that row instead (valid in every dialect this
    # project generates for — ORDER BY evaluates after the SELECT list),
    # rather than duplicating agg-expression-building logic here. Only
    # applies when has_group_by — a bare scalar aggregate with no grouping
    # (single result row) orders fine by the raw column, exactly as before.
    if has_group_by:
        for row in (select_rows or []):
            if row["table_fqn"] == table_fqn and row["column_name"] == column_name and row.get("aggregation"):
                return [{"alias": row["alias"], "direction": order.get("direction") or "DESC"}], None

    return [{
        "table_fqn": table_fqn, "column_name": column_name,
        "direction": order.get("direction") or "DESC",
    }], None


# ---------------------------------------------------------------------------
# Step 5 — JOIN planning
# ---------------------------------------------------------------------------

def _build_joins(join_plan: dict) -> tuple[list[dict], list[str]]:
    """
    One joins[] entry per trusted (path_found=True) step already in
    query_plan["join_plan"]["steps"] — those steps are, by construction,
    sourced only from analyze_join_quality/recommend_best_join_path, which
    only ever see AUTO/APPROVED relationships (Phase 2's _load_edges
    filter). A step with path_found=False is never emitted as a join; it is
    instead reported so the caller can hard-block the plan (Step 8).
    """
    joins: list[dict] = []
    untrusted: list[str] = []
    for step in join_plan.get("steps") or []:
        if not step.get("path_found"):
            untrusted.append(f"{step.get('from_table')} -> {step.get('to_table')}")
            continue
        joins.append({
            "join_type":    step.get("join_type"),
            "left_table":   step.get("from_table"),
            "left_column":  step.get("from_column"),
            "right_table":  step.get("to_table"),
            "right_column": step.get("to_column"),
            "cardinality":  step.get("cardinality"),
            "fanout_risk":  step.get("fanout_risk"),
            "confidence":   step.get("confidence"),
        })
    return joins, untrusted


# ---------------------------------------------------------------------------
# Step 6 — WHERE planning
# ---------------------------------------------------------------------------

def _build_where(filters: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Only query_plan["filters"] entries already resolved against a known
    column (Phase 3's resolved=True) are eligible. No raw user SQL is ever
    accepted: operator must be in the whitelist and the value must not be
    shaped like a SQL injection attempt. A failing filter is dropped from
    `where` and reported for Step 8's blocking check — never silently
    passed through, never silently dropped without a trace.
    """
    where: list[dict] = []
    rejected: list[str] = []
    for f in filters:
        column = f.get("column") or f.get("field")
        operator = f.get("operator")
        value = f.get("value")

        if not f.get("resolved"):
            continue  # already reported as unknown_filter_column by query_planning_service

        if operator not in _ALLOWED_OPERATORS:
            rejected.append(f"{column}: operator '{operator}' is not allowed")
            continue
        if _is_unsafe_value(value) or _is_unsafe_value(column):
            rejected.append(f"{column}: value looks like raw SQL and was rejected")
            continue

        where.append({
            "table_fqn":   f.get("table_fqn"),
            "column_name": column,
            "operator":    operator,
            "value":       value,
        })
    return where, rejected


# ---------------------------------------------------------------------------
# Step 7 — GROUP BY planning
# ---------------------------------------------------------------------------

def _build_group_by(select: list[dict]) -> list[dict]:
    """All non-aggregated (dimension) columns must be grouped whenever at
    least one aggregated measure is present — prevents an invalid mixed
    aggregate/non-aggregate query. No measures selected -> nothing to group.

    Always carries the row's `alias` alongside `column_name`: for a plain
    dimension the two are identical, but for a calendar-grain dimension
    (`time_grain` set) the SELECT/GROUP BY clause projects under the grain
    alias (e.g. "start_year" for `YEAR(StartDate)`), never the logical
    column name — downstream result validation checks the alias, since
    that's the only thing the returned rows actually contain."""
    has_measure = any(row["aggregation"] for row in select)
    if not has_measure:
        return []
    return [
        (
            {"table_fqn": row["table_fqn"], "column_name": row["column_name"],
             "time_grain": row["time_grain"], "alias": row["alias"]}
            if row.get("time_grain") else
            {"table_fqn": row["table_fqn"], "column_name": row["column_name"], "alias": row["alias"]}
        )
        for row in select if not row["aggregation"]
    ]


# ---------------------------------------------------------------------------
# Step 8 — PII/approval validation (the one new call this module makes)
# ---------------------------------------------------------------------------

def _is_pii_flagged(dic: dict | None, prof: dict | None) -> bool:
    """Single source of truth for 'does this column carry a PII signal',
    shared by the Step 8 block check and the list_entities safe-column
    filter below — deliberately the same two flags, so there is only ever
    one place that decides what counts as PII in this module."""
    return bool((prof and prof.get("pii_name_heuristic")) or (dic and dic.get("pii_risk")))


def _check_pii_and_approval(
    source_id: int, user_id: str, select: list[dict], allow_unconfirmed_pii: bool,
) -> tuple[list[str], list[dict]]:
    """
    Per selected column: structured PII/approval state via
    business_knowledge_service.get_column_business_context — precise flags,
    not a parse of query_plan's free-text warning strings. Bounded to the
    columns actually selected (never the full candidate set).
    """
    pii_blocks: list[str] = []
    warnings: list[dict] = []
    for row in select:
        ctx = get_column_business_context(source_id, user_id, row["table_fqn"], row["column_name"])
        if ctx is None:
            continue
        dic = ctx.get("dictionary")
        prof = ctx.get("profiling")

        if not dic or not dic.get("is_approved"):
            warnings.append({
                "type": "metadata_not_approved", "severity": "LOW",
                "message": f"{row['table_fqn']}.{row['column_name']} has no approved dictionary entry.",
            })

        if _is_pii_flagged(dic, prof):
            confirmed  = bool(prof and prof.get("pii_confirmed"))
            aggregation = row.get("aggregation")
            warnings.append({
                "type": "pii_involved", "severity": "MEDIUM" if confirmed else "HIGH",
                "message": (
                    f"{row['table_fqn']}.{row['column_name']} may contain PII "
                    f"({'confirmed' if confirmed else 'unconfirmed'})."
                ),
            })
            # Aggregated columns (COUNT/SUM/AVG/etc.) never return raw cell values —
            # the PII flag on the source column does not apply to the aggregate output.
            if not aggregation and not confirmed and not allow_unconfirmed_pii:
                pii_blocks.append(f"{row['table_fqn']}.{row['column_name']} (unconfirmed PII)")

    return pii_blocks, warnings


# Priority tiers for auto-selected bare-entity-list columns — lower sorts
# first. Built entirely from existing dictionary/profiling flags already
# returned by get_table_business_context; no new classification.
_LIST_ENTITY_TIER_IDENTIFIER = 0
_LIST_ENTITY_TIER_BUSINESS_LABEL = 1
_LIST_ENTITY_TIER_STATUS = 2
_LIST_ENTITY_TIER_DATE = 3
_LIST_ENTITY_TIER_DIMENSION = 4
_LIST_ENTITY_TIER_OTHER = 5


def _list_entity_column_tier(dic: dict | None, schema: dict | None) -> int:
    if (schema and schema.get("is_primary_key")) or (dic and dic.get("is_id")):
        return _LIST_ENTITY_TIER_IDENTIFIER
    if dic and dic.get("business_label"):
        return _LIST_ENTITY_TIER_BUSINESS_LABEL
    if dic and dic.get("semantic_type") == "STATUS":
        return _LIST_ENTITY_TIER_STATUS
    if dic and dic.get("is_date"):
        return _LIST_ENTITY_TIER_DATE
    if dic and dic.get("is_dimension"):
        return _LIST_ENTITY_TIER_DIMENSION
    return _LIST_ENTITY_TIER_OTHER


def _select_safe_list_entity_columns(
    source_id: int, user_id: str, table_fqn: str, known_column_names: list[str],
) -> list[str]:
    """
    Choose which of a bare-entity-list table's known columns are safe to
    auto-select — the user asked to "list clients", not for any specific
    column, so nothing PII-flagged (confirmed or unconfirmed) should enter
    the SELECT without being explicitly requested by name. One bounded
    get_table_business_context call for this single already-resolved table
    (never per column, never for the full candidate set) supplies the same
    dictionary/profiling flags Step 8 already reads elsewhere in this
    module. Preference order: identifier, business label, status, date,
    other dimensions, then any other safe column — ties keep known_columns'
    original order. Capped at _MAX_LIST_ENTITY_COLUMNS, same as before.
    """
    ctx = get_table_business_context(source_id, user_id, table_fqn)
    col_ctx_by_name = {c["column_name"]: c for c in (ctx.get("columns") or [])} if ctx else {}

    safe: list[tuple[int, int, str]] = []
    for idx, col_name in enumerate(known_column_names):
        col_ctx = col_ctx_by_name.get(col_name)
        dic = col_ctx.get("dictionary") if col_ctx else None
        prof = col_ctx.get("profiling") if col_ctx else None
        schema = col_ctx.get("schema") if col_ctx else None
        if _is_pii_flagged(dic, prof):
            continue
        safe.append((_list_entity_column_tier(dic, schema), idx, col_name))

    safe.sort(key=lambda t: (t[0], t[1]))
    return [col_name for _, _, col_name in safe[:_MAX_LIST_ENTITY_COLUMNS]]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_sql_plan(
    source_id: int,
    user_id: str,
    query_plan: dict,
    *,
    allow_unconfirmed_pii: bool = False,
) -> dict:
    """
    Step 2. Transform plan_business_query()'s output into a structured,
    SQL-ready plan. Never produces a SQL string, never executes anything.

    query_plan is trusted as the caller's own prior plan_business_query()
    output — source ownership was already verified when it was built, so
    this function does not re-verify it. The only DB reads here are bounded
    get_column_business_context calls, one per selected column.

    query_plan being None (e.g. caller's plan_business_query returned None
    for an unowned/unknown source) is itself a hard validation failure
    rather than a crash.
    """
    if not query_plan:
        return {
            "select": [], "from": None, "joins": [], "where": [],
            "group_by": [], "order_by": [], "distinct": False, "limits": {},
            "warnings": [],
            "validation": {
                "valid": False, "read_only": True, "checks": {},
                "blocking_reasons": ["No query plan was provided."],
            },
            "explanation": ["No query plan was provided — nothing to build."],
            "semantic_context": [],
        }

    checks: dict[str, bool] = {}
    blocking_reasons: list[str] = []
    warnings: list[dict] = list(query_plan.get("warnings") or [])
    explanation: list[str] = []

    known_columns = query_plan.get("columns") or {}
    intent = query_plan.get("intent") or {}
    aggregation = intent.get("aggregation")
    distinct_requested = bool(intent.get("distinct"))
    order_intent = intent.get("order")

    measure_rows, unresolved_measures = _select_entries(
        query_plan.get("measures") or [], aggregation, distinct=distinct_requested,
    )
    dimension_rows, unresolved_dimensions = _select_entries(query_plan.get("dimensions") or [], None)
    select = measure_rows + dimension_rows

    # --- Bare entity list routing (Enterprise AI Analyst Agent) -----------
    # query_planning_service._build_intent routes a confidently-resolved,
    # single bare-concept question ("Show clients") to intent.type ==
    # "list_entities". Concepts never carry a column_name (they resolve to a
    # whole table, not a metric/dimension column), so SELECT is built here
    # from that table's own known columns instead of from measures/
    # dimensions — capped at _MAX_LIST_ENTITY_COLUMNS rather than an
    # unbounded SELECT *. A6.1: the column pool itself is pre-filtered to
    # exclude anything PII-flagged (confirmed or unconfirmed) via
    # _select_safe_list_entity_columns, since these columns were never
    # explicitly requested by name — only the resulting safe columns reach
    # Step 8's PII/approval check below, same as any other selected column.
    # joined_detail_list (Enterprise AI Analyst Agent) — the mirror-image
    # question ("names of the students"): same single-bare-concept routing
    # as list_entities above, plus the one dimension query_planning_service
    # already resolved on a table joined via a declared, trusted
    # relationship (e.g. dnnuser.Users.FirstName via
    # dbo.ADF_Student.StudentUserID -> dnnuser.Users.UserID) appended to the
    # concept table's own known columns.
    if intent.get("type") in ("list_entities", "joined_detail_list") and not select:
        resolved_list_concepts = [c for c in (query_plan.get("concepts") or []) if c.get("selected")]
        if len(resolved_list_concepts) == 1:
            list_entity_table = resolved_list_concepts[0]["selected"]["table_fqn"]
            raw_known_columns = known_columns.get(list_entity_table) or []
            safe_column_names = _select_safe_list_entity_columns(
                source_id, user_id, list_entity_table, raw_known_columns,
            )
            select = [
                {
                    "table_fqn": list_entity_table, "column_name": col_name,
                    "alias": col_name, "aggregation": None, "distinct": False,
                }
                for col_name in safe_column_names
            ]
            if intent.get("type") == "joined_detail_list":
                select += dimension_rows
            if raw_known_columns and not safe_column_names:
                warnings.append({
                    "type": "no_safe_columns_available", "severity": "LOW",
                    "message": (
                        f"{list_entity_table} has no columns safe to auto-select — every "
                        "known column is PII-flagged or otherwise blocked by governance "
                        "metadata. Refusing to select any column rather than expose "
                        "sensitive data that was never explicitly requested."
                    ),
                })

    # --- Relationship list routing (Day 3, Task 1) -------------------------
    # query_planning_service._try_build_relationship_plan routes a bare "show
    # <entity> and their <entity>[, and <entity>]" question — 2-3 fully-
    # grounded entities, no measure/dimension — to intent.type ==
    # "relationship_list". Mirrors the list_entities/joined_detail_list
    # carve-out above (each entity's own known columns, PII-filtered via the
    # same _select_safe_list_entity_columns), just for every grounded entity
    # in query_plan["concepts"] instead of requiring exactly one. Aliases are
    # prefixed with the owning entity's name — unlike the single-entity carve
    # -out above, 2-3 different tables commonly share a column name (Id,
    # Status, CreatedDate, ...), and an unqualified alias collision would
    # silently overwrite one entity's value with another's in the result row
    # (exactly the "silent dropping" Day 3 explicitly forbids), not just look
    # untidy.
    if intent.get("type") == "relationship_list" and not select:
        for concept in query_plan.get("concepts") or []:
            sel = concept.get("selected")
            if not sel:
                continue
            entity_table = sel["table_fqn"]
            entity_prefix = re.sub(r"\s+", "_", (concept.get("term") or entity_table).strip().lower())
            raw_known_columns = known_columns.get(entity_table) or []
            safe_column_names = _select_safe_list_entity_columns(
                source_id, user_id, entity_table, raw_known_columns,
            )
            select += [
                {
                    "table_fqn": entity_table, "column_name": col_name,
                    "alias": f"{entity_prefix}__{col_name}", "aggregation": None, "distinct": False,
                }
                for col_name in safe_column_names
            ]
            if raw_known_columns and not safe_column_names:
                warnings.append({
                    "type": "no_safe_columns_available", "severity": "LOW",
                    "message": (
                        f"{entity_table} has no columns safe to auto-select — every "
                        "known column is PII-flagged or otherwise blocked by governance "
                        "metadata. Refusing to select any column rather than expose "
                        "sensitive data that was never explicitly requested."
                    ),
                })

    # --- Aggregation Plan (Milestone Phase 6.2) ---------------------------
    # Purely informational passthrough — explicitly records the
    # aggregation-shape decision query_planning_service._resolve_entity_count
    # already made (and any join-fan-out adjustment already applied) so it
    # is traceable on the SQL plan itself, the same way "semantic_context"
    # was added in M-4. Does not affect select/joins/where/validation
    # construction; at most one entity-count measure is expected per
    # question by construction (one COUNT per query).
    aggregation_plan: dict | None = None
    for entry in query_plan.get("measures") or []:
        sel = entry.get("selected")
        if sel and sel.get("aggregation_target") in ("entity_count", "distinct_entity_count"):
            aggregation_plan = {
                "aggregation_target":   sel["aggregation_target"],
                "counted_entity":       sel.get("business_label") or sel["table_fqn"].split(".")[-1],
                "counted_table":        sel["table_fqn"],
                "counted_column":       sel.get("column_name"),
                "distinct":             bool(sel.get("distinct")),
                "fanout_risk":          (query_plan.get("join_plan") or {}).get("fanout_risk"),
                "key_tier":             sel.get("key_tier"),
                "key_confidence":       sel.get("key_confidence"),
                "key_selection_reason": sel.get("key_selection_reason"),
            }
            break

    # --- Semantic Correctness Guard (Milestone Phase 6.1) ----------------
    # A measure/dimension whose winning candidate failed the term-vs-column
    # concept-family check in query_planning_service._resolve_term carries a
    # semantic_compatibility payload with compatible=False. This is a hard,
    # unconditional block regardless of whether other terms resolved — a
    # wrong-but-present answer is worse than a refusal, so it does not get
    # the "some terms resolved, skip the rest" leniency unresolved/ambiguous
    # terms receive below.
    semantic_failures = [
        entry["semantic_compatibility"]
        for entry in (query_plan.get("measures") or []) + (query_plan.get("dimensions") or [])
        if entry.get("semantic_compatibility")
    ]
    checks["semantic_compatible"] = not semantic_failures
    for fail in semantic_failures:
        blocking_reasons.append(
            f"Semantic incompatibility: requested '{fail['requested_measure']}' resolved to "
            f"{fail['resolved_concept']} ({fail['column_family']}) but the term implies a "
            f"{fail['term_family']} concept — refusing to guess. " + (
                f"Suggested instead: {fail['suggested']['table_fqn']}.{fail['suggested']['column_name']}."
                if fail.get("suggested") else
                "No confident alternative was found."
            )
        )

    # --- ambiguity ------------------------------------------------------
    unresolved_terms = unresolved_measures + unresolved_dimensions
    checks["no_ambiguous_unresolved_terms"] = not unresolved_terms
    if unresolved_terms:
        if select:
            # Day 2C follow-up ("material qualifier policy") — a term with
            # ZERO candidates at all (missing_{kind}) has nothing a
            # clarification could ever offer, so it is promoted straight to
            # a hard refusal here — the same "never silently drop a
            # material qualifier" principle Step 6a below already applies
            # to date/status filters specifically, now generalized to any
            # measure/dimension term. Verified live against real CCPP:
            # "How many clients have a phone number on file?" used to
            # silently become "how many clients are there at all", with no
            # disclosed limitation. An AMBIGUOUS term (>=1 real candidate,
            # however weak) is deliberately left on the soft warning below
            # instead — core.orchestrator.context_builder._extract_
            # ambiguous_terms now independently evaluates every such entry
            # (a sibling Day 2C follow-up fix removing that function's own
            # "bail out if anything else resolved" early return) and offers
            # it as a clarification question BEFORE this function ever
            # runs, for every caller that wires clarification; the one
            # caller that doesn't (execute_query_route, per its own
            # documented no-clarification-handling) keeps this soft warning
            # as its existing, unchanged fallback.
            #
            # Excludes a term that ALSO already resolved as a bare business
            # concept (query_plan["concepts"]) — extract_terms() puts every
            # "before"-clause word into both concepts and measures, so a
            # bare-entity question ("names of the students") predictably
            # re-tries "students" as a metric COLUMN name after it already
            # resolved fine as the concept/table itself, and predictably
            # finds nothing (missing_measure) purely as a byproduct of that
            # dual-classification — not a real dropped qualifier. Mirrors
            # core.orchestrator.context_builder._extract_ambiguous_terms'
            # own resolved_concept_terms exclusion for the identical reason.
            resolved_concept_terms = {
                (c.get("term") or "").lower()
                for c in (query_plan.get("concepts") or [])
                if c.get("selected")
            }
            # A metric-ranking Top N/Bottom N question ("Top 10 sales by
            # amount") is structurally ambiguous for its "by X" phrase:
            # extract_terms() always classifies X as a DIMENSION term (a
            # GROUP BY candidate), but the SAME phrase is exactly as likely
            # to mean "ranked/ordered by X" — already resolved separately,
            # from `measures`, by _resolve_ranking_order_column below (Step
            # "Ordering"). Reproduced against a real fixture: "amount" is a
            # metric column, never matches as a dimension, and blocking on
            # it would refuse a perfectly well-formed ranking question
            # (tests/test_composer_sql_routing.py::
            # test_top_n_generates_order_by_and_tightened_limit). Only a
            # DATE-targeted order ("most recently added") is exempted from
            # this carve-out — that shape resolves its date column via a
            # completely separate path (_find_date_column_with_hint) that
            # never touches `dimensions` at all, so an unresolved dimension
            # alongside it is never this same ambiguity.
            metric_ranking_active = bool(order_intent) and order_intent.get("target") != "date"

            def _is_unusable(entry: dict, kind: str) -> bool:
                warning_types = {w.get("type") for w in (entry.get("warnings") or [])}
                if f"missing_{kind}" in warning_types:
                    return True
                if f"ambiguous_{kind}" in warning_types:
                    # core.orchestrator.context_builder._extract_ambiguous_
                    # terms' own mirrored rule: a MULTI-candidate list where
                    # EVERY entry scores exactly 0 is a fake tie, not a
                    # genuine clarification opportunity — functionally
                    # identical to missing_{kind}, so it gets the same hard-
                    # refusal treatment here. A SINGLE candidate at score 0
                    # is deliberately NOT included — that is the pre-
                    # existing Phase 2 design (a bare-entity-count term
                    # against the only table in the database is a
                    # legitimate, if weak, single-guess clarification,
                    # handled by _extract_ambiguous_terms upstream, not a
                    # dropped qualifier this function needs to refuse).
                    candidates = entry.get("candidates") or []
                    return len(candidates) >= 2 and not any((c.get("score") or 0) > 0 for c in candidates)
                return False

            missing_terms = [
                entry["term"]
                for kind, entries in (
                    ("measure", query_plan.get("measures") or []),
                    ("dimension", query_plan.get("dimensions") or []),
                )
                for entry in entries
                if entry.get("selected") is None
                and _is_unusable(entry, kind)
                and (entry.get("term") or "").lower() not in resolved_concept_terms
                and not (kind == "dimension" and metric_ranking_active)
            ]
            remaining = [t for t in unresolved_terms if t not in missing_terms]
            if missing_terms:
                blocking_reasons.append(
                    "Term(s) could not be matched to any field, and dropping them "
                    f"would change the meaning of the question: {', '.join(missing_terms)}."
                )
            if remaining:
                warnings.append({
                    "type": "unresolved_terms", "severity": "LOW",
                    "message": (
                        f"Some terms could not be resolved and were skipped: "
                        f"{', '.join(remaining)}."
                    ),
                })
        else:
            blocking_reasons.append(
                f"Unresolved term(s) cannot be planned: {', '.join(unresolved_terms)}."
            )

    # --- no SELECT * (select must be non-empty and column-specific) ------
    checks["select_not_empty"] = bool(select)
    if not select:
        blocking_reasons.append("No measures or dimensions resolved — refusing to plan an empty/SELECT * query.")

    # --- every referenced column must exist in query_plan["columns"] -----
    # column_name is None only for the synthesized COUNT(*) row-count entry
    # (query_planning_service._resolve_count_all) — there is no column
    # reference to validate there, only the already-trusted table_fqn.
    missing = [
        f"{r['table_fqn']}.{r['column_name']}" for r in select
        if r["column_name"] is not None and not _columns_known(r["table_fqn"], r["column_name"], known_columns)
    ]
    checks["all_columns_exist"] = not missing
    if missing:
        blocking_reasons.append(f"Column(s) not found in query plan: {', '.join(missing)}.")

    # --- Step 4: FROM ------------------------------------------------------
    join_plan = query_plan.get("join_plan") or {}
    primary_table = join_plan.get("primary_table")
    if not primary_table:
        tables = join_plan.get("tables") or []
        primary_table = tables[0] if tables else (select[0]["table_fqn"] if select else None)

    alias_pool: set[str] = set()
    from_clause = (
        {"table_fqn": primary_table, "alias": _short_alias(primary_table, alias_pool)}
        if primary_table else None
    )
    checks["from_table_resolved"] = from_clause is not None
    if from_clause is None:
        blocking_reasons.append("No driving table could be determined for FROM.")

    # --- Step 5: JOINs -------------------------------------------------------
    joins, untrusted_joins = _build_joins(join_plan)
    checks["all_joins_trusted"] = not untrusted_joins
    if untrusted_joins:
        blocking_reasons.append(
            f"No trusted (AUTO/APPROVED) join path found for: {', '.join(untrusted_joins)}."
        )
    for j in joins:
        if j.get("fanout_risk") in ("MEDIUM", "HIGH"):
            warnings.append({
                "type": "high_fanout_risk" if j["fanout_risk"] == "HIGH" else "fanout_risk",
                "severity": "HIGH" if j["fanout_risk"] == "HIGH" else "MEDIUM",
                "message": (
                    f"Join {j['left_table']}.{j['left_column']} -> "
                    f"{j['right_table']}.{j['right_column']} has {j['fanout_risk']} fan-out risk."
                ),
            })

    # --- Step 6: WHERE -------------------------------------------------------
    where, rejected_filters = _build_where(query_plan.get("filters") or [])
    checks["no_invalid_filters"] = not rejected_filters
    if rejected_filters:
        blocking_reasons.append(f"Invalid filter(s) rejected: {'; '.join(rejected_filters)}.")

    # --- Step 6a: requested date/status filter that couldn't be located ------
    # query_planning_service emits date_column_not_found/status_column_not_found
    # as an informational warning when the question explicitly asked for a
    # time- or status-scoped answer ("last year", "open job orders") but no
    # matching column was found among the resolved tables — previously this
    # was non-blocking, and generation would silently proceed as if the
    # question had no such constraint at all. That's a materially different,
    # wrong answer, not a safe degradation: refuse instead, the same
    # never-silently-drop-a-reference principle the out-of-graph guard below
    # already applies to columns that WERE found on the wrong table.
    dropped_filter_types = {"date_column_not_found", "status_column_not_found"}
    dropped_filters = [w for w in warnings if w.get("type") in dropped_filter_types]
    checks["no_dropped_requested_filters"] = not dropped_filters
    for w in dropped_filters:
        blocking_reasons.append(w["message"])

    # --- Step 6b: Plan-integrity guard — table membership (Phase 6.1) --------
    # Every selected measure/dimension/filter must reference a table that is
    # actually part of the FROM/JOIN graph just built. query_planning_service
    # now scopes date/status filter discovery to the join graph itself (Phase
    # 2, Step 2), so this guard should no longer trip on that specific
    # defect (SQL Server previously rejected the resulting SQL as "multi-part
    # identifier could not be bound") — kept as a hard backstop for any other
    # path that could still produce an out-of-graph reference. Never
    # auto-joins the missing table and never silently drops the offending
    # reference — hard block only.
    graph_tables: set[str] = set()
    if from_clause:
        graph_tables.add(from_clause["table_fqn"])
    for j in joins:
        graph_tables.add(j["left_table"])
        graph_tables.add(j["right_table"])

    out_of_graph = sorted({
        f"{row['table_fqn']}.{row['column_name']}" for row in select
        if row.get("column_name") is not None and row.get("table_fqn") not in graph_tables
    } | {
        f"{w['table_fqn']}.{w['column_name']}" for w in where
        if w.get("table_fqn") not in graph_tables
    })
    checks["all_references_in_query_graph"] = not out_of_graph
    if out_of_graph and from_clause is not None:
        blocking_reasons.append(
            f"Reference(s) to table(s) outside the FROM/JOIN graph: {', '.join(out_of_graph)}. "
            "Refusing to generate SQL rather than silently dropping the reference or "
            "auto-joining an unrelated table."
        )

    # --- Step 7: GROUP BY ------------------------------------------------
    group_by = _build_group_by(select)

    # --- Step 9: ORDER BY (Top N / Bottom N / Latest / Earliest) ---------
    order_by, order_warning = _build_order_by(order_intent, known_columns, measure_rows, bool(group_by))
    if order_warning:
        warnings.append({"type": "order_column_not_resolved", "severity": "LOW", "message": order_warning})

    # --- Step 6b (continued): order_by must also stay inside the FROM/JOIN
    # graph. _build_order_by runs after the Step 6b guard above (it needs
    # group_by, computed after that guard), so a raw {table_fqn, column_name}
    # row here was never checked against graph_tables — an alias-only row
    # (aggregated GROUP BY case) references its own already-checked SELECT
    # row and needs no re-check. Same hard-block treatment as select/where:
    # never silently dropped, never auto-joined.
    order_out_of_graph = sorted({
        f"{row['table_fqn']}.{row['column_name']}" for row in order_by
        if row.get("table_fqn") and row.get("table_fqn") not in graph_tables
    })
    if order_out_of_graph:
        checks["all_references_in_query_graph"] = False
        blocking_reasons.append(
            f"Reference(s) to table(s) outside the FROM/JOIN graph: {', '.join(order_out_of_graph)}. "
            "Refusing to generate SQL rather than silently dropping the reference or "
            "auto-joining an unrelated table."
        )

    # Default chronological ordering for a time-grain GROUP BY ("students by
    # year") when the question itself requested no explicit ordering — reads
    # naturally oldest-to-newest rather than in undefined row order. Only
    # applies when nothing else already claimed order_by.
    if not order_by and group_by:
        grain_group = next((g for g in group_by if g.get("time_grain")), None)
        if grain_group:
            grain_row = next(
                (
                    r for r in select
                    if r["table_fqn"] == grain_group["table_fqn"] and r["column_name"] == grain_group["column_name"]
                ),
                None,
            )
            if grain_row:
                order_by = [{"alias": grain_row["alias"], "direction": "ASC"}]

    # Query-level DISTINCT (e.g. "distinct students") only applies when the
    # query has no aggregation at all — an aggregated row already has its
    # own per-row DISTINCT handling (see _select_entries' `distinct` field,
    # rendered as COUNT(DISTINCT col) downstream), so stacking a second,
    # query-level DISTINCT on top of it would be redundant/invalid SQL.
    query_level_distinct = distinct_requested and not aggregation

    # --- Step 8: PII / approval -------------------------------------------
    pii_blocks: list[str] = []
    if select:
        pii_blocks, pii_warnings = _check_pii_and_approval(
            source_id, user_id, select, allow_unconfirmed_pii
        )
        warnings.extend(pii_warnings)
    checks["no_unconfirmed_pii"] = not pii_blocks
    if pii_blocks:
        blocking_reasons.append(
            f"Unconfirmed PII column(s) blocked (pass allow_unconfirmed_pii=True to override): "
            f"{', '.join(pii_blocks)}."
        )

    checks["read_only"] = True  # structural guarantee — this layer has no write concept

    valid = not blocking_reasons

    # Narrative prose only — deliberately avoids spelling out SQL clause
    # keywords/syntax (SELECT/FROM/JOIN ON/GROUP BY), even informally, so
    # this stays a structured-plan explanation rather than SQL-shaped text.
    if select:
        described = ", ".join(
            (
                f"{r['aggregation'].lower()} of {r['table_fqn']}.{r['column_name']}" if r["aggregation"] and r["column_name"]
                else f"row count of {r['table_fqn']}" if r["aggregation"] and not r["column_name"]
                else f"{r['table_fqn']}.{r['column_name']}"
            )
            for r in select
        )
        explanation.append(f"Resolved columns: {described}.")
    if from_clause:
        explanation.append(f"Driving table: {from_clause['table_fqn']}.")
    for j in joins:
        explanation.append(
            f"Links to {j['right_table']} via {j['left_table']}.{j['left_column']} "
            f"matching {j['right_table']}.{j['right_column']} "
            f"({j['join_type'] or 'INNER'}-style, cardinality {j['cardinality']}, "
            f"fan-out risk {j['fanout_risk']})."
        )
    if where:
        explanation.append(f"{len(where)} validated filter(s) will be applied.")
    if group_by:
        explanation.append(f"Results will be grouped by {len(group_by)} dimension(s).")
    if order_by:
        order_ref = (
            order_by[0]["alias"] if order_by[0].get("alias") and not order_by[0].get("table_fqn")
            else f"{order_by[0]['table_fqn']}.{order_by[0]['column_name']}"
        )
        explanation.append(f"Ordered by {order_ref} ({order_by[0]['direction']}).")
    if not valid:
        explanation.append(f"BLOCKED: {' '.join(blocking_reasons)}")

    # The safety row cap (1000) still applies even when a smaller Top-N/
    # Bottom-N limit was requested — the requested limit can only ever
    # tighten the cap, never loosen it.
    requested_limit = (order_intent or {}).get("limit") if valid else None
    row_limit = min(requested_limit, 1000) if requested_limit else (1000 if valid else None)

    return {
        "select":   select,
        "from":     from_clause,
        "joins":    joins,
        "where":    where,
        "group_by": group_by,
        "order_by": order_by,
        "distinct": query_level_distinct,
        "limits":   {"row_limit": row_limit} if valid else {},
        "warnings": warnings,
        "validation": {
            "valid": valid,
            "read_only": True,
            "checks": checks,
            "blocking_reasons": blocking_reasons,
        },
        "explanation": explanation,
        # Milestone M-4 — Enterprise Semantic Resolution: pass through the
        # resolved business-concept context plan_business_query already
        # attached under "concepts", so the SQL Planner's own output carries
        # resolved semantic context rather than only isolated table
        # candidates. Pure passthrough — does not affect select/joins/where.
        "semantic_context": query_plan.get("concepts", []),
        # Milestone Phase 6.2 — Aggregation Shape Correctness: explicit,
        # traceable record of the aggregation-shape decision (entity vs.
        # measure count, counted table/column, key tier/confidence/reason,
        # fan-out risk) for entity-count questions. None for every other
        # question shape (SUM/AVG/MIN/MAX/plain list) — pure passthrough,
        # does not affect select/joins/where/validation.
        "aggregation_plan": aggregation_plan,
    }
