"""
Strict SQL Server read-only query validation — SQL rebuild Phase 3, Step 2,
extended by Phase 5, Step 10 (column-level validation).

Validates a candidate SQL statement (from core.sqlservice.
sql_generation_service.SqlGenerationResult) using a REAL T-SQL parser
(sqlglot, dialect="tsql") — never regex-only matching. This is the same
category of check core/live/query_validator.py already performs for a
different, unrelated track (comment rejection, single-statement
enforcement, write-keyword scanning), but built on an actual AST instead
of string/keyword heuristics, and additionally cross-checking every
referenced object (and, as of Step 10, every referenced COLUMN) against
the caller-supplied schema context.

Thin orchestration over one external library (sqlglot — including its
optimizer.scope module for real scope/alias/CTE/subquery analysis) plus
two existing typed results — it does not duplicate anything from
core.sqlservice.sql_generation_service or core.sqlservice.
schema_context_retrieval, and it never re-derives or re-selects schema
context; it only reads `schema_context.documents` to resolve/verify
object AND column references.

Scope: this module NEVER executes SQL, never connects to a live SQL
Server or SQLite database, never calls OpenAI, and never modifies or
"repairs" the candidate SQL — a rejected candidate is simply reported as
rejected, with reasons, for the caller (a later step, not this one) to
decide what to do next.

Known boundary (do not exceed): this module validates STRUCTURE (statement
shape, forbidden constructs, referenced tables/views), REFERENCE existence
(does each referenced object appear in schema_context), and — Step 10 —
COLUMN existence/qualification/ambiguity for every column reference the
query makes, all from schema_context's own structured metadata
(ContextDocument.metadata["column_names"]) — NEVER from document_text
prose, which is not reliably re-parseable (see the "Column-level
validation" section below for the fail-closed rule this implies). This
module still never validates column DATA TYPES, computed-expression
semantics, or whether a query is logically correct/meaningful — only that
every table and column it names actually, unambiguously exists where the
query says it does.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.dialects.tsql import TSQL as _TSqlDialect
from sqlglot.expressions import SAFE_IDENTIFIER_RE
from sqlglot.optimizer.scope import Scope, traverse_scope

from core.config import (
    SQL_VALIDATION_MAX_AST_NODES,
    SQL_VALIDATION_MAX_COLUMN_REFERENCES,
    SQL_VALIDATION_MAX_CTES,
    SQL_VALIDATION_MAX_JOINS,
    SQL_VALIDATION_MAX_REFERENCED_OBJECTS,
    SQL_VALIDATION_MAX_SQL_CHARACTERS,
)
from core.sqlservice.business_knowledge_evidence import load_business_knowledge_evidence
from core.sqlservice.intent_evidence import ReviewedIntentEvidence, material_requirements
from core.sqlservice.schema_context_retrieval import STATUS_OK as _SCHEMA_CONTEXT_STATUS_OK
from core.sqlservice.schema_context_retrieval import SchemaContextResult
from core.sqlservice.sql_generation_service import STATUS_OK as _SQL_GENERATION_STATUS_OK
from core.sqlservice.sql_generation_service import SqlGenerationResult

__all__ = [
    "STATUS_OK",
    "STATUS_INVALID_CANDIDATE",
    "STATUS_REJECTED",
    "ValidatedColumnReference",
    "SqlValidationResult",
    "validate_sql_candidate",
    "TSQL_DIALECT",
    "FORBIDDEN_NODE_TYPES",
    "BANNED_ROWSET_FUNCTIONS",
    "ALLOWED_ROOT_TYPES",
    "contains_sql_comment_marker",
    "parse_single_read_only_statement",
    "find_forbidden_construct_reasons",
    "find_structural_table_reference_reasons",
    "render_tsql_identifier",
]

STATUS_OK = "OK"
STATUS_INVALID_CANDIDATE = "INVALID_CANDIDATE"  # nothing to validate: upstream result wasn't a usable SQL candidate
STATUS_REJECTED = "REJECTED"  # there WAS a candidate SQL string, but it failed structural/read-only/reference validation

# Public — reused by core.sqlservice.sql_example_model.normalize_sql_template
# (and any other module that needs the SAME parser-backed structural/read-
# only authority this module already established) so no second, divergent
# notion of "is this a safe read-only T-SQL statement" ever exists in this
# codebase. See parse_single_read_only_statement/find_forbidden_construct_
# reasons/find_structural_table_reference_reasons below — the exact,
# schema-independent subset of this module's own checks.
TSQL_DIALECT = "tsql"

# Any occurrence of these node types ANYWHERE in the parsed tree is
# rejected — not just at the root. sqlglot's tsql grammar is more lenient
# than real SQL Server syntax and will happily parse e.g. a DELETE as a
# CTE body (`WITH x AS (DELETE ...) SELECT * FROM x`), which real SQL
# Server would reject at parse time — so this module cannot trust a
# root-type check alone and must walk the entire tree.
FORBIDDEN_NODE_TYPES: tuple[type, ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Execute, exp.Set, exp.Transaction, exp.Commit, exp.Rollback,
    exp.Declare, exp.Command, exp.Use,
)

# T-SQL table-valued rowset functions that reach outside the declared
# schema context (a file, a linked server, an ad-hoc connection string) —
# sqlglot has no dedicated node type for these; they parse as a generic
# Anonymous function call.
BANNED_ROWSET_FUNCTIONS = frozenset({"OPENROWSET", "OPENQUERY", "OPENDATASOURCE"})

# Root statement types this module ever accepts, before the recursive scan
# above even runs — SetOperation covers UNION/UNION ALL/EXCEPT/INTERSECT
# uniformly (sqlglot's shared base class for all of them).
ALLOWED_ROOT_TYPES = (exp.Select, exp.SetOperation)


# ── Shared, schema-independent parser authority ────────────────────────────
#
# The three functions below are the single source of parser-backed truth
# for "is this a single, well-formed, read-only T-SQL SELECT/set-operation
# statement, structurally" — extracted, not duplicated, from
# validate_sql_candidate's own original inline logic so a second module
# (core.sqlservice.sql_example_model.normalize_sql_template, which never
# has a schema_context to resolve object existence against) can reuse the
# EXACT same authority rather than re-implementing any part of it with
# regex or its own ad hoc AST walk. validate_sql_candidate below calls
# these same functions; nothing about their behavior changed by extracting
# them into public names.


# T-SQL single-word reserved keywords, as sqlglot's OWN TSQL tokenizer
# already knows them (never a hand-maintained list) — reused below purely
# to decide whether an identifier NEEDS bracket-quoting to avoid colliding
# with a keyword token, on top of sqlglot's own SAFE_IDENTIFIER_RE shape
# check (which alone would leave e.g. a table literally named "TABLE" or
# "DATE" unquoted, since it satisfies the bare-identifier character
# pattern despite also being a reserved word).
_TSQL_RESERVED_KEYWORDS = frozenset(k.upper() for k in _TSqlDialect.tokenizer_class.KEYWORDS.keys())


def render_tsql_identifier(name: str) -> str:
    """
    Render one T-SQL identifier safely and deterministically, reusing
    sqlglot's own identifier-safety detection (SAFE_IDENTIFIER_RE, the same
    check exp.to_identifier() itself uses) and its own TSQL-dialect
    bracket-quoting/escaping — never a hand-rolled, regex-only parallel
    resolver.

    A plain identifier (letters/digits/underscore, not starting with a
    digit, not a T-SQL reserved keyword) is rendered BARE — e.g. "Widget"
    stays "Widget", so ordinary generated SQL is completely unaffected by
    this function's existence. Anything else — a space, punctuation, a
    reserved keyword collision, or (the case that motivated this function)
    a name that itself contains literal SQL Server quote-bracket
    characters — is rendered bracket-quoted, with every literal ']'
    correctly doubled to ']]' per T-SQL's own escaping rule.

    This round-trips exactly: re-parsing the rendered text with
    sqlglot(dialect="tsql") always recovers the ORIGINAL, unrendered `name`
    byte-for-byte as the parsed Identifier's own value. That is what makes
    a canonical object_fqn/column name safe to embed directly into
    generated SQL text without ever creating a second, SQL-rendered
    identity distinct from the caller's own canonical spelling — the exact
    bug this function fixes: a raw canonical name that happens to already
    contain '[' / ']' characters (a real, if unusual, SQL Server table
    name — not presentation syntax) previously got embedded verbatim into
    generated SQL, which sqlglot then mis-parsed as bracket-QUOTING syntax
    around a shorter, DIFFERENT bare name, causing the parsed-and-re-
    resolved identity to silently diverge from the original canonical one.

    Raises ValueError for a non-string or empty name — a caller/programmer
    contract violation, never a routine condition.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"identifier name must be a non-empty string, got {name!r}")
    needs_quoting = name.upper() in _TSQL_RESERVED_KEYWORDS or not SAFE_IDENTIFIER_RE.match(name)
    return exp.to_identifier(name, quoted=needs_quoting).sql(dialect=TSQL_DIALECT)


def contains_sql_comment_marker(sql: str) -> bool:
    """True when `sql` contains a line (`--`) or block (`/*`) SQL comment
    marker anywhere — checked as plain text, BEFORE parsing, because a
    comment could otherwise hide a blocked keyword from the parser-based
    checks below (e.g. `SEL/**/ECT`). This is the one place either module
    ever treats raw text as authoritative, and only for this one cheap,
    conservative pre-filter — never for statement STRUCTURE."""
    return "--" in sql or "/*" in sql


def parse_single_read_only_statement(
    sql: str, *, max_sql_characters: int, max_ast_nodes: int,
) -> tuple[exp.Expression | None, str | None]:
    """
    The statement-shape gate: length, comment marker, parse success,
    single-statement, AST size, and root-statement-type — in that order,
    stopping at the first failure. Returns (tree, None) on success, or
    (None, reason) on the first problem found. Never inspects table
    references or forbidden constructs deeper in the tree — see
    find_forbidden_construct_reasons/find_structural_table_reference_reasons
    for those, run separately by the caller once a tree is in hand.
    """
    if len(sql) > max_sql_characters:
        return None, f"SQL length {len(sql)} exceeds the maximum of {max_sql_characters} characters"
    if contains_sql_comment_marker(sql):
        return None, "SQL comments are not permitted"

    try:
        statements = [s for s in sqlglot.parse(sql, read=TSQL_DIALECT) if s is not None]
    except Exception as exc:
        return None, f"SQL could not be parsed: {exc}"

    if len(statements) != 1:
        return None, f"expected exactly one SQL statement, found {len(statements)}"
    tree = statements[0]

    node_count = sum(1 for _ in tree.walk())
    if node_count > max_ast_nodes:
        return None, f"AST node count {node_count} exceeds the maximum of {max_ast_nodes}"

    if not isinstance(tree, ALLOWED_ROOT_TYPES):
        return None, f"statement type {type(tree).__name__!r} is not a read-only SELECT or set operation"

    return tree, None


def find_forbidden_construct_reasons(tree: exp.Expression) -> tuple[str, ...]:
    """
    Walk the ENTIRE tree (not just the root) for a forbidden statement type
    anywhere (a write hidden inside a CTE body — sqlglot's tsql grammar
    parses this more permissively than real SQL Server syntax ever would),
    a SELECT INTO, or a banned table-valued rowset function
    (OPENROWSET/OPENQUERY/OPENDATASOURCE). Returns every reason found, not
    just the first — empty tuple means none found.
    """
    reasons: list[str] = []
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            reasons.append(f"{type(node).__name__} is not permitted")
        elif isinstance(node, exp.Select) and node.args.get("into") is not None:
            reasons.append("SELECT INTO is not permitted")
        elif isinstance(node, exp.Anonymous) and isinstance(node.this, str) and node.this.upper() in BANNED_ROWSET_FUNCTIONS:
            reasons.append(f"{node.this.upper()} is not permitted")
    return tuple(reasons)


def find_structural_table_reference_reasons(tree: exp.Expression) -> tuple[str, ...]:
    """
    The schema-INDEPENDENT subset of _resolve_table_references' own checks
    below: temp tables, table variables/linked-server four-part references,
    and cross-database/linked-server (catalog-qualified) references need no
    schema context to reject outright — only "does this object actually
    exist" does (that half stays in _resolve_table_references, which also
    needs the schema-context object index). Deliberately duplicated here
    rather than having _resolve_table_references delegate to this function,
    to avoid any behavior-change risk to that already-tested code path —
    matching this codebase's established convention of keeping a small,
    schema-independent pure check zero-coupled from its schema-aware
    superset (see core.sqlservice.sql_example_vector_index's own
    duplication of core.sqlservice.vector_index's normalize helper, for the
    same reason). Reused by core.sqlservice.sql_example_model.
    normalize_sql_template, which never has a schema context available.
    """
    cte_names_lower = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    reasons: list[str] = []
    for table in tree.find_all(exp.Table):
        catalog = table.catalog or ""
        db = table.db or ""
        name = table.name or ""

        if not catalog and not db and name.lower() in cte_names_lower:
            continue

        if not isinstance(table.this, exp.Identifier):
            reasons.append(
                f"table reference {table.sql(dialect=TSQL_DIALECT)!r} is not a plain table name "
                f"(a table variable or a linked-server/four-part reference)"
            )
            continue

        if table.this.args.get("temporary"):
            reasons.append(f"temp table reference '#{name}' is not permitted")
            continue

        if catalog:
            reasons.append(
                f"cross-database or linked-server reference '{catalog}.{db}.{name}' is not permitted"
            )
    return tuple(reasons)


@dataclass(frozen=True)
class ValidatedColumnReference:
    """
    One physical column a validated candidate genuinely references — SQL
    rebuild Phase 5, Step 10. `object_fqn` is always a REAL canonical
    object_fqn from schema_context.documents (never a CTE/derived-table
    alias — a column that resolves only through a CTE or derived table is
    still existence-checked, but does not itself produce one of these;
    the physical column(s) underneath it already do, from wherever that
    CTE/derived table's own body validated them). `column_name` is the
    canonical spelling from that object's own structured column_names
    metadata — never the SQL's own casing.
    """

    object_fqn: str
    column_name: str


@dataclass(frozen=True)
class SqlValidationResult:
    """
    Result of one validate_sql_candidate() call.

    status: STATUS_OK | STATUS_INVALID_CANDIDATE | STATUS_REJECTED.
        STATUS_INVALID_CANDIDATE means there was no SQL to validate at all
        (the generation result/schema context wasn't itself successful, or
        the candidate needs clarification) — a routine upstream-state
        condition, not a validation failure. STATUS_REJECTED means real SQL
        text WAS validated and failed — see `error` for every reason found
        (not just the first).
    canonical_referenced_object_fqns: only meaningful when status is
        STATUS_OK — the exact object_fqn spellings from schema_context.
        documents (never the SQL's or the candidate's own casing), sorted.
    statement_type: the root AST node's type name (e.g. "Select", "Union",
        "Except", "Intersect") — only meaningful when status is STATUS_OK.
    validated_column_references: Step 10 addition. Only meaningful when
        status is STATUS_OK — every distinct physical (object_fqn,
        column_name) pair the candidate references anywhere (SELECT
        projections, WHERE, JOIN/ON, GROUP BY, HAVING, ORDER BY, window
        expressions, function/aggregate arguments, CASE branches, CTEs,
        subqueries, UNION branches, and wildcard expansion), deduplicated
        and sorted deterministically by (object_fqn, column_name). Empty
        tuple `()` on every other status, and also legitimately for an OK
        result whose SQL references no physical column at all (e.g.
        `SELECT 1`). Never a mutable list/dict — see ValidatedColumnReference.
    """

    status: str
    sql: str
    canonical_referenced_object_fqns: tuple[str, ...]
    statement_type: str | None
    build_id: int | None
    schema_fingerprint: str | None
    warnings: tuple[str, ...]
    error: str | None
    validated_column_references: tuple[ValidatedColumnReference, ...] = ()


def _invalid_candidate_result(
    generation_result: SqlGenerationResult, sql: str, reason: str,
) -> SqlValidationResult:
    return SqlValidationResult(
        status=STATUS_INVALID_CANDIDATE,
        sql=sql,
        canonical_referenced_object_fqns=(),
        statement_type=None,
        build_id=generation_result.build_id,
        schema_fingerprint=generation_result.schema_fingerprint,
        warnings=generation_result.warnings,
        error=reason,
    )


def _rejected_result(
    generation_result: SqlGenerationResult, sql: str, reasons: list[str],
) -> SqlValidationResult:
    return SqlValidationResult(
        status=STATUS_REJECTED,
        sql=sql,
        canonical_referenced_object_fqns=(),
        statement_type=None,
        build_id=generation_result.build_id,
        schema_fingerprint=generation_result.schema_fingerprint,
        warnings=generation_result.warnings,
        error="; ".join(reasons),
    )


# ── Schema-context object index (built once per call) ─────────────────────


def _build_object_index(schema_context: SchemaContextResult) -> tuple[dict[str, str], dict[str, list[str]]]:
    """
    fqn_by_lower: lowercased "schema.table" -> canonical object_fqn, for
    exact (schema-qualified) lookups.
    bare_name_index: lowercased bare table name -> list of canonical
    object_fqns whose table part matches it — more than one entry means an
    unqualified reference to that name is genuinely ambiguous.
    """
    fqn_by_lower: dict[str, str] = {}
    bare_name_index: dict[str, list[str]] = {}
    for document in schema_context.documents:
        fqn = document.object_fqn
        fqn_by_lower[fqn.lower()] = fqn
        _, _, bare_name = fqn.rpartition(".")
        bare_name_index.setdefault(bare_name.lower(), []).append(fqn)
    return fqn_by_lower, bare_name_index


# ── Table-reference extraction/resolution ─────────────────────────────────


def _resolve_table_references(
    tree: exp.Expression,
    cte_names_lower: set[str],
    fqn_by_lower: dict[str, str],
    bare_name_index: dict[str, list[str]],
) -> tuple[set[str], list[str]]:
    """Returns (resolved canonical object_fqns, rejection reasons)."""
    resolved: set[str] = set()
    reasons: list[str] = []

    for table in tree.find_all(exp.Table):
        catalog = table.catalog or ""
        db = table.db or ""
        name = table.name or ""

        # A CTE self-reference is never a real database object — SQL
        # Server never allows schema-qualifying a CTE reference, so this
        # is only ever mistaken for a real object when it's completely
        # unqualified.
        if not catalog and not db and name.lower() in cte_names_lower:
            continue

        if not isinstance(table.this, exp.Identifier):
            reasons.append(
                f"table reference {table.sql(dialect=TSQL_DIALECT)!r} is not a plain table name "
                f"(a table variable or a linked-server/four-part reference)"
            )
            continue

        if table.this.args.get("temporary"):
            reasons.append(f"temp table reference '#{name}' is not permitted")
            continue

        if catalog:
            reasons.append(
                f"cross-database or linked-server reference '{catalog}.{db}.{name}' is not permitted"
            )
            continue

        if db:
            canonical = fqn_by_lower.get(f"{db}.{name}".lower())
            if canonical is None:
                reasons.append(f"table '{db}.{name}' is not present in the supplied schema context")
            else:
                resolved.add(canonical)
        else:
            candidates = bare_name_index.get(name.lower(), [])
            if not candidates:
                reasons.append(f"table '{name}' is not present in the supplied schema context")
            elif len(candidates) > 1:
                reasons.append(
                    f"unqualified table '{name}' is ambiguous — matches multiple schemas: {sorted(candidates)}"
                )
            else:
                resolved.add(candidates[0])

    return resolved, reasons


# ── Column-level validation (SQL rebuild Phase 5, Step 10) ─────────────────
#
# Scope-aware, sqlglot AST-only — never regex. Built entirely on top of
# sqlglot.optimizer.scope.traverse_scope/Scope, the same real parser
# authority this module already uses for everything else, never a second,
# hand-rolled notion of "what is visible here". Runs ONLY after every
# existing table-level check above has already passed cleanly (requirement
# 14: zero behavior change to any pre-Step-10 rejection path — column
# validation simply never runs for a candidate that was already going to
# be rejected for an earlier reason).
#
# Design (see this module's own review notes / the Step 10 report for the
# full rationale):
#   - The authoritative column catalog (_build_column_catalog) is built
#     ONLY from schema_context.documents' structured `column_names`
#     metadata — never document_text prose. A table/view in
#     resolved_fqns (i.e. actually referenced by the SQL) whose metadata
#     is missing, wrongly typed, empty, or contains a blank entry is
#     UNTRUSTWORTHY — requirement 4's fail-closed rule — and rejects the
#     whole candidate before any scope walk even starts.
#   - Every scope from traverse_scope() gets two PURELY LOCAL, order-
#     independent lookups computed for it up front: its own output column
#     names (_compute_output_columns — what an ancestor sees if it
#     references this scope as a CTE/derived table) and its own visible
#     sources (_resolve_visible_sources — alias -> physical object_fqn or
#     -> another scope, by id()). Neither depends on any OTHER scope
#     having been processed yet, so there is no ordering hazard even
#     though traverse_scope's bottom-up order does not strictly guarantee
#     siblings are visited in dependency order relative to each other.
#   - A QUALIFIED column resolves against its own scope's visible sources
#     first, then walks scope.parent/scope.parent.parent/... (sqlglot's
#     own real ancestor-scope chain) — this is what makes a genuinely
#     correlated qualified reference (`t.ID` inside a subquery, where `t`
#     is only visible in an outer scope) resolve correctly, never a
#     hand-rolled correlation heuristic.
#   - An UNQUALIFIED column resolves against its own LOCAL scope FIRST —
#     zero local matches is not yet a rejection, 2+ local matches is
#     ambiguous and rejected immediately (a local column always shadows
#     an outer one of the same name; local ambiguity never falls back
#     outward), exactly one local match wins outright. Step 10A: only
#     when the local scope has ZERO matches does resolution walk
#     scope.parent/scope.parent.parent/... one level at a time — at each
#     ancestor level, exactly one match there resolves the reference and
#     STOPS (the nearest scope that supplies a match wins), 2+ matches at
#     that SAME level is ambiguous and rejected immediately without
#     continuing further out, and zero matches moves on to the next
#     ancestor. The chain exhausted with no scope ever producing a match
#     is UNKNOWN. See _resolve_unqualified_column's own docstring for the
#     precise algorithm — legal correlated unqualified references (e.g. a
#     bare column inside a correlated EXISTS/scalar subquery that only
#     exists on the outer table) now resolve correctly, never guessed at
#     and never silently rejected.
#   - The ONE exception: an unqualified name inside its own scope's own
#     ORDER BY that matches NOTHING as a real column, but DOES match that
#     scope's own SELECT-list output alias, is accepted as a T-SQL
#     ORDER-BY-alias reference — never added to validated_column_references
#     (it is not a physical column) and never confused with one.
#   - Wildcards: `alias.*` expands to every column of that real visible
#     alias. Bare `SELECT *` is accepted ONLY when the scope has exactly
#     one visible source (so the output column set is unambiguous and
#     fully known from the catalog) — see _validate_scope_wildcards' own
#     docstring for the explicit boundary. `COUNT(*)` is untouched by any
#     of this — sqlglot parses it as Count(this=Star()), never an
#     exp.Column at all, so it is structurally invisible to both the
#     wildcard scan (which only inspects top-level SELECT-list Star/
#     Column-of-Star entries) and the per-column walk.


def _build_column_catalog(schema_context: SchemaContextResult) -> dict[str, dict[str, str] | None]:
    """
    The authoritative column catalog, keyed by canonical object_fqn (as
    already spelled in schema_context.documents — never re-cased).
    Requirement 3/4: built ONLY from each document's structured
    `column_names` metadata field, never `document_text` prose.

    A value of None means "this object has no trustworthy structured
    column metadata" — requirement 4's fail-closed signal, checked by the
    caller against resolved_fqns before any scope analysis runs; never a
    silent empty-catalog stand-in that would make every column on that
    table look "unknown" instead of making the whole candidate rejected
    outright. "Trustworthy" means metadata["column_names"] is present, is
    a list/tuple, is non-empty, and every element is a non-blank string.

    Each trustworthy entry maps lowercased-column-name -> canonical
    spelling (the exact casing already stored in metadata, never the
    SQL's own casing) — case-insensitive lookup, canonical spelling
    preserved, mirroring _build_object_index's own object-level
    convention exactly.
    """
    catalog: dict[str, dict[str, str] | None] = {}
    for document in schema_context.documents:
        raw = document.metadata.get("column_names")
        if (
            not isinstance(raw, (list, tuple)) or not raw
            or not all(isinstance(c, str) and c.strip() for c in raw)
        ):
            catalog[document.object_fqn] = None
            continue
        catalog[document.object_fqn] = {c.lower(): c for c in raw}
    return catalog


def _physical_table_fqn(
    table: exp.Table, fqn_by_lower: dict[str, str], bare_name_index: dict[str, list[str]],
) -> str | None:
    """
    Resolve a Table node scope analysis has already confirmed is a genuine
    physical reference (never a CTE self-reference — sqlglot's own scope
    resolution already made that distinction: see _resolve_visible_sources'
    isinstance(source, exp.Table) vs Scope check) to its canonical
    object_fqn — the exact same qualified/bare-unique rule
    _resolve_table_references already applied. By the time this runs,
    table-level validation has already confirmed every such reference
    resolves unambiguously, so this is a plain re-lookup, never a second
    ambiguity check; None here would indicate an internal inconsistency
    (defensive only, never expected in practice).
    """
    db = table.db or ""
    name = table.name or ""
    if db:
        return fqn_by_lower.get(f"{db}.{name}".lower())
    candidates = bare_name_index.get(name.lower(), [])
    return candidates[0] if len(candidates) == 1 else None


def _compute_output_columns(scope: Scope) -> tuple[str, ...]:
    """
    This scope's own exposed output column names — for use by whatever
    ancestor scope references it as a CTE or derived-table alias. Purely
    local to `scope`'s own AST (never depends on any other scope having
    been processed first): an explicit CTE/derived-table column list
    (`AS x (A, B)`) always wins when present (requirement 9's "explicit
    CTE column lists"); otherwise each SELECT-list expression's own
    alias_or_name, in projection order. A UNION/EXCEPT/INTERSECT scope
    exposes its FIRST branch's own names (T-SQL's own rule for a set
    operation's result column names).
    """
    parent_node = scope.expression.parent
    if isinstance(parent_node, (exp.CTE, exp.Subquery)):
        alias_node = parent_node.args.get("alias")
        if alias_node is not None:
            explicit_columns = alias_node.args.get("columns") or []
            if explicit_columns:
                return tuple(c.name for c in explicit_columns)

    expr = scope.expression
    while isinstance(expr, exp.SetOperation):
        expr = expr.this
    if isinstance(expr, exp.Select):
        return tuple(e.alias_or_name for e in expr.expressions)
    return ()


def _resolve_visible_sources(
    scope: Scope, fqn_by_lower: dict[str, str], bare_name_index: dict[str, list[str]],
) -> dict[str, tuple[str, object]]:
    """
    alias (lowercased, bracket-stripped — sqlglot's own .name/.table
    properties already strip T-SQL [bracket] quoting, so bracketed
    identifiers need no special-casing here at all) ->
    ("physical", canonical_object_fqn) | ("derived", id(child_scope)).
    Purely structural: the table-vs-CTE/derived-table distinction is
    sqlglot's own scope analysis (isinstance(source, exp.Table) vs Scope),
    never re-derived here. A source resolved to neither (unsupported
    construct) is simply absent from the returned mapping — a column
    qualified against it correctly reports "invalid alias" downstream,
    never silently skipped.
    """
    visible: dict[str, tuple[str, object]] = {}
    for alias, (_node, source) in scope.selected_sources.items():
        alias_lower = alias.lower()
        if isinstance(source, exp.Table):
            fqn = _physical_table_fqn(source, fqn_by_lower, bare_name_index)
            if fqn is not None:
                visible[alias_lower] = ("physical", fqn)
        elif isinstance(source, Scope):
            visible[alias_lower] = ("derived", id(source))
    return visible


def _is_in_order_by(column: exp.Column, scope_expression: exp.Expression) -> bool:
    """True when `column` sits inside `scope_expression`'s own ORDER BY —
    the one clause where a T-SQL SELECT-list alias reference is valid
    (requirement 9). Walks column.parent upward only until reaching
    scope_expression itself, so a column belonging to a DIFFERENT
    (nested) scope's ORDER BY is never mistaken for this scope's own."""
    node = column.parent
    while node is not None and node is not scope_expression:
        if isinstance(node, exp.Order):
            return True
        node = node.parent
    return False


_COLUMN_STATUS_INVALID_ALIAS = "INVALID_ALIAS"
_COLUMN_STATUS_UNKNOWN = "UNKNOWN_COLUMN"
_COLUMN_STATUS_OK_PHYSICAL = "OK_PHYSICAL"
_COLUMN_STATUS_OK_DERIVED = "OK_DERIVED"


def _resolve_qualified_column(
    scope: Scope,
    qualifier_lower: str,
    column_name: str,
    visible_by_scope_id: dict[int, dict[str, tuple[str, object]]],
    output_columns_by_scope_id: dict[int, tuple[str, ...]],
    column_catalog: dict[str, dict[str, str] | None],
) -> tuple:
    """
    Walks `scope`, then scope.parent, scope.parent.parent, ... — sqlglot's
    own real ancestor-scope chain — until `qualifier_lower` resolves in
    SOME ancestor's own visible sources, or the chain is exhausted
    (requirement 9's "correlated outer-scope references"). Returns:
      (_COLUMN_STATUS_INVALID_ALIAS,)
      (_COLUMN_STATUS_UNKNOWN, object_fqn_or_None)
      (_COLUMN_STATUS_OK_PHYSICAL, object_fqn, canonical_column_name)
      (_COLUMN_STATUS_OK_DERIVED,)
    """
    current: Scope | None = scope
    while current is not None:
        entry = visible_by_scope_id[id(current)].get(qualifier_lower)
        if entry is not None:
            kind, ref = entry
            if kind == "physical":
                columns = column_catalog.get(ref)
                canonical = columns.get(column_name.lower()) if columns else None
                if canonical is None:
                    return (_COLUMN_STATUS_UNKNOWN, ref)
                return (_COLUMN_STATUS_OK_PHYSICAL, ref, canonical)
            output_names_lower = {n.lower() for n in output_columns_by_scope_id.get(ref, ())}
            if column_name.lower() not in output_names_lower:
                return (_COLUMN_STATUS_UNKNOWN, None)
            return (_COLUMN_STATUS_OK_DERIVED,)
        current = current.parent
    return (_COLUMN_STATUS_INVALID_ALIAS,)


def _matches_in_scope_sources(
    column_name: str,
    visible: dict[str, tuple[str, object]],
    output_columns_by_scope_id: dict[int, tuple[str, ...]],
    column_catalog: dict[str, dict[str, str] | None],
) -> list[tuple]:
    """
    Every visible source in ONE scope's own `visible` mapping that has a
    column of this name — (alias, kind, object_fqn_or_None,
    canonical_or_matched_name) per match. Zero, one (a clean match), or 2+
    (ambiguous within that one scope — including two aliases of the SAME
    physical table in a self-join, which genuinely IS ambiguous in real
    T-SQL) matches. Purely a single-scope lookup; see
    _resolve_unqualified_column below for the ancestor-walking caller
    that applies this at each scope level in turn (Step 10A).
    """
    name_lower = column_name.lower()
    matches: list[tuple] = []
    for alias_lower, (kind, ref) in visible.items():
        if kind == "physical":
            columns = column_catalog.get(ref)
            canonical = columns.get(name_lower) if columns else None
            if canonical is not None:
                matches.append((alias_lower, "physical", ref, canonical))
        else:
            for name in output_columns_by_scope_id.get(ref, ()):
                if name.lower() == name_lower:
                    matches.append((alias_lower, "derived", ref, name))
                    break
    return matches


_UNQUALIFIED_STATUS_UNKNOWN = "UNKNOWN"
_UNQUALIFIED_STATUS_AMBIGUOUS = "AMBIGUOUS"
_UNQUALIFIED_STATUS_OK = "OK"


def _resolve_unqualified_column(
    scope: Scope,
    column_name: str,
    visible_by_scope_id: dict[int, dict[str, tuple[str, object]]],
    output_columns_by_scope_id: dict[int, tuple[str, ...]],
    column_catalog: dict[str, dict[str, str] | None],
) -> tuple:
    """
    Step 10A correlated-reference algorithm for an UNQUALIFIED column:
      1. Check `scope`'s own LOCAL visible sources first. Exactly one
         match resolves locally (a local column always shadows an outer
         one of the same name — never even looks further out). Two or
         more local matches is ambiguous and rejected immediately —
         "never fall back to an outer scope" on local ambiguity.
      2. Only when the LOCAL scope has ZERO matches does this walk
         scope.parent, scope.parent.parent, ... (sqlglot's own real
         ancestor-scope chain) — nearest ancestor first.
      3. At EACH ancestor level: exactly one match there resolves the
         reference and STOPS (the nearest scope that supplies a match
         wins, never merged with/preferred over a farther one); 2+
         matches at that SAME level is ambiguous and rejected
         immediately, without continuing further out; zero matches moves
         on to the next ancestor.
      4. The chain exhausted with no scope ever producing a match is
         UNKNOWN.

    Returns:
      (_UNQUALIFIED_STATUS_OK, kind, object_fqn_or_None, canonical_or_matched_name)
      (_UNQUALIFIED_STATUS_AMBIGUOUS, sorted_alias_list)
      (_UNQUALIFIED_STATUS_UNKNOWN,)
    """
    current: Scope | None = scope
    while current is not None:
        matches = _matches_in_scope_sources(
            column_name, visible_by_scope_id[id(current)], output_columns_by_scope_id, column_catalog,
        )
        if len(matches) == 1:
            _, kind, ref, canonical = matches[0]
            return (_UNQUALIFIED_STATUS_OK, kind, ref, canonical)
        if len(matches) > 1:
            return (_UNQUALIFIED_STATUS_AMBIGUOUS, sorted({m[0] for m in matches}))
        current = current.parent
    return (_UNQUALIFIED_STATUS_UNKNOWN,)


def _expand_wildcard(
    kind: str, ref: object, column_catalog: dict[str, dict[str, str] | None],
) -> list[tuple[str, str]]:
    """(object_fqn, canonical_column_name) for every column of one
    visible source — physical only; a derived/CTE source's own columns
    were already validated when THAT scope was itself checked, so
    expanding a wildcard over one adds nothing further to the physical
    reference list."""
    if kind != "physical":
        return []
    columns = column_catalog.get(ref)
    if not columns:
        return []  # unreachable: trustworthiness for every resolved fqn is already checked before any scope walk
    return [(ref, canonical) for canonical in columns.values()]


def _validate_scope_wildcards(
    scope: Scope, visible: dict[str, tuple[str, object]], column_catalog: dict[str, dict[str, str] | None],
) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Requirement 10 — the wildcard policy:
      - `alias.*` is permitted whenever `alias` is a real visible source
        in this scope, expanded to every one of its columns.
      - bare `SELECT *` is permitted ONLY when this scope has EXACTLY ONE
        visible source. With one source, the output column set is fully
        known (from the same trustworthy catalog every other check in
        this module already relies on) and unambiguous, so the boundary
        this function documents (requirement 10's "explicitly document
        the boundary... reject it if reliable output-column validation
        cannot be guaranteed") is exactly: MULTIPLE visible sources means
        the wildcard's own eventual column set/order depends on join
        semantics this module does not attempt to reproduce, so it is
        rejected rather than guessed at.
      - `COUNT(*)` never reaches this function at all — see this
        section's own module-level comment.
    """
    if not isinstance(scope.expression, exp.Select):
        return [], []

    refs: list[tuple[str, str]] = []
    reasons: list[str] = []
    for projection in scope.expression.expressions:
        if isinstance(projection, exp.Star):
            if len(visible) != 1:
                reasons.append(
                    f"bare SELECT * is only permitted with exactly one visible table/view/CTE in scope "
                    f"(found {len(visible)}) — output columns cannot be reliably validated otherwise"
                )
                continue
            ((kind, ref),) = visible.values()
            refs.extend(_expand_wildcard(kind, ref, column_catalog))
        elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            alias_lower = (projection.table or "").lower()
            entry = visible.get(alias_lower)
            if entry is None:
                reasons.append(f"'{projection.table}.*' does not reference a visible table/view/CTE alias")
                continue
            kind, ref = entry
            refs.extend(_expand_wildcard(kind, ref, column_catalog))
    return refs, reasons


def _validate_columns(
    tree: exp.Expression,
    resolved_fqns: set[str],
    fqn_by_lower: dict[str, str],
    bare_name_index: dict[str, list[str]],
    schema_context: SchemaContextResult,
    max_column_references: int,
) -> tuple[tuple[ValidatedColumnReference, ...], tuple[str, ...]]:
    """
    The Step 10 orchestrator. Only ever called after table-level
    validation has already fully succeeded. Returns
    (validated_column_references, reasons) — reasons non-empty means
    reject; validated_column_references is only meaningful when reasons
    is empty.
    """
    column_catalog = _build_column_catalog(schema_context)

    untrustworthy = sorted(fqn for fqn in resolved_fqns if column_catalog.get(fqn) is None)
    if untrustworthy:
        return (), tuple(
            f"table/view {fqn!r} lacks trustworthy structured column metadata (no usable column_names) "
            f"— cannot safely validate its columns" for fqn in untrustworthy
        )

    try:
        scopes = list(traverse_scope(tree))
    except Exception as exc:
        return (), (f"query scope could not be analyzed for column validation: {exc}",)

    output_columns_by_scope_id: dict[int, tuple[str, ...]] = {id(s): _compute_output_columns(s) for s in scopes}
    visible_by_scope_id: dict[int, dict[str, tuple[str, object]]] = {
        id(s): _resolve_visible_sources(s, fqn_by_lower, bare_name_index) for s in scopes
    }

    physical_refs: set[tuple[str, str]] = set()
    reasons: list[str] = []

    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            continue  # a SetOperation wrapper scope has no columns of its own to check

        visible = visible_by_scope_id[id(scope)]
        select_output_aliases_lower = {
            name.lower() for name in output_columns_by_scope_id[id(scope)] if name and name != "*"
        }

        wildcard_refs, wildcard_reasons = _validate_scope_wildcards(scope, visible, column_catalog)
        physical_refs.update(wildcard_refs)
        reasons.extend(wildcard_reasons)

        for column in scope.find_all(exp.Column):
            if isinstance(column.this, exp.Star):
                continue  # alias.* already handled by _validate_scope_wildcards above

            name = column.name
            qualifier = column.table

            if qualifier:
                result = _resolve_qualified_column(
                    scope, qualifier.lower(), name, visible_by_scope_id, output_columns_by_scope_id, column_catalog,
                )
                if result[0] == _COLUMN_STATUS_INVALID_ALIAS:
                    reasons.append(
                        f"'{qualifier}' is not a valid table/view/CTE alias in this scope "
                        f"(referenced as '{qualifier}.{name}')"
                    )
                elif result[0] == _COLUMN_STATUS_UNKNOWN:
                    fqn = result[1]
                    where = f" on {fqn!r}" if fqn else ""
                    reasons.append(f"column '{qualifier}.{name}' does not exist{where}")
                elif result[0] == _COLUMN_STATUS_OK_PHYSICAL:
                    physical_refs.add((result[1], result[2]))
                # _COLUMN_STATUS_OK_DERIVED: existence already confirmed; nothing further to record
            else:
                result = _resolve_unqualified_column(
                    scope, name, visible_by_scope_id, output_columns_by_scope_id, column_catalog,
                )
                if result[0] == _UNQUALIFIED_STATUS_OK:
                    _, kind, ref, canonical = result
                    if kind == "physical":
                        physical_refs.add((ref, canonical))
                elif result[0] == _UNQUALIFIED_STATUS_UNKNOWN:
                    if _is_in_order_by(column, scope.expression) and name.lower() in select_output_aliases_lower:
                        continue  # a valid T-SQL ORDER BY reference to a SELECT-list alias, never a physical column
                    reasons.append(f"column '{name}' does not exist on any visible table/view/CTE in this scope or an outer scope")
                else:  # _UNQUALIFIED_STATUS_AMBIGUOUS
                    reasons.append(f"column '{name}' is ambiguous — matches multiple visible objects: {result[1]}")

    if reasons:
        return (), tuple(reasons)

    if len(physical_refs) > max_column_references:
        return (), (
            f"resolved column-reference count {len(physical_refs)} exceeds the maximum of {max_column_references}",
        )

    ordered = tuple(
        ValidatedColumnReference(object_fqn=fqn, column_name=col) for fqn, col in sorted(physical_refs)
    )
    return ordered, ()


# ── Deterministic confirmed-relationship JOIN validation (Phase 7, Step 3A,
#    Part 3) ─────────────────────────────────────────────────────────────
#
# Verified Step 3 live-evaluation failure this section closes: AI generation
# and AI review both accepted a JOIN between two real, known objects on a
# same-named column pair that had NO confirmed relationship behind it
# (dbo.EDU_ClassSignups.ClassID = dbo.vw_StudentCirriculum.ClassID — two
# genuinely different, unrelated ClassID columns). Column EXISTENCE alone
# (what _validate_columns above already checks) can never catch this: both
# columns are real. This section adds the missing check — every JOIN
# between two DISTINCT PHYSICAL objects in the executed query must be
# backed by a confirmed relationship's own COLUMN PAIR, never merely a
# shared column name, an object-level-only relationship mention, or a
# document-text/prose reference. AI review is never trusted for this: this
# validator runs unconditionally, after review, and its rejection can never
# be overridden.
#
# Trust source: ONLY core.sqlservice.retrieval_documents.build_retrieval_
# documents()'s own "confirmed_relationship_edges" metadata key — the same
# structured, already-classify_relationship_row()-fail-closed-confirmed
# data core.sqlservice.relationship_graph already builds its own confirmed-
# only adjacency from (see that module's docstring: "Never
# 'inferred_relationship_*' ... by construction, not merely by a runtime
# filter"). WEAK/INFERRED/PENDING/REJECTED relationships are never present
# in this key at all — classify_relationship_row() already excludes them
# fail-closed at document-build time, long before this validator ever runs.
# document_text prose is never consulted — a relationship mentioned only in
# a RELATIONSHIPS_INFERRED_* text line (or not classified as confirmed for
# any other reason) simply has no corresponding confirmed_relationship_edges
# entry, so a join relying on it is correctly rejected as unsupported.
#
# Column-pair, not object-pair: confirmed_relationship_edges always carries
# BOTH the document's own column and the other side's "schema.table.column"
# descriptor together (see retrieval_documents.py's build_retrieval_
# documents(): every confirmed row requires both from_column and to_column
# to be present before it is ever recorded at all) — so an object PAIR that
# has SOME confirmed relationship via a DIFFERENT column pair than the one
# the SQL actually joins on is correctly rejected too ("wrong columns on a
# confirmed object pair").


def _build_confirmed_column_edges(schema_context: SchemaContextResult) -> frozenset:
    """
    Undirected {(object_fqn_lower, column_lower), (object_fqn_lower,
    column_lower)} edges, one frozenset-of-2 per confirmed relationship —
    built ONLY from schema_context.documents[*].metadata["confirmed_
    relationship_edges"] (never confirmed_relationship_targets/sources
    alone, which lack the document's OWN column and so cannot recover a
    column-level pair; never document_text). An edge whose "other"
    descriptor doesn't parse into a genuine "schema.table.column" shape, or
    whose own column is blank, is silently excluded — fail closed, never
    guessed — exactly like this module's existing column-catalog
    trustworthiness policy.

    A caller checking whether SQL's `A.ca = B.cb` is confirmed tests
    `frozenset({(a_fqn_lower, ca_lower), (b_fqn_lower, cb_lower)}) in
    edges` — symmetric by construction, so a reversed relationship
    direction or reversed equality operand order both match identically.
    """
    edges: set[frozenset] = set()
    for document in schema_context.documents:
        own_fqn_lower = document.object_fqn.lower()
        for edge in document.metadata.get("confirmed_relationship_edges") or ():
            own_column = str(edge.get("column") or "").strip().lower()
            other_descriptor = str(edge.get("other") or "").strip()
            other_fqn, sep, other_column = other_descriptor.rpartition(".")
            other_column = other_column.strip().lower()
            if not own_column or not sep or not other_fqn or not other_column:
                continue
            edges.add(frozenset({(own_fqn_lower, own_column), (other_fqn.lower(), other_column)}))
    return frozenset(edges)


def _resolve_equality_operand(
    column: exp.Column,
    scope: Scope,
    visible_by_scope_id: dict[int, dict[str, tuple[str, object]]],
    output_columns_by_scope_id: dict[int, tuple[str, ...]],
    column_catalog: dict[str, dict[str, str] | None],
) -> tuple[str, str] | None:
    """
    Resolve one side of an `=` comparison to (object_fqn_lower,
    column_lower) ONLY when it is a genuine reference to a real PHYSICAL
    column (reusing the exact same qualified/unqualified/ancestor-scope
    resolution _validate_columns already trusts — see
    _resolve_qualified_column/_resolve_unqualified_column above). Returns
    None for anything else (an alias that isn't a valid physical/derived
    source, a derived-table/CTE output column, an ambiguous or unknown
    unqualified reference, an ORDER-BY-alias reference, ...) — such an
    operand simply contributes no confirmable edge, it is never guessed at.
    """
    if column.table:
        result = _resolve_qualified_column(
            scope, column.table.lower(), column.name, visible_by_scope_id, output_columns_by_scope_id, column_catalog,
        )
        if result[0] == _COLUMN_STATUS_OK_PHYSICAL:
            return (result[1].lower(), result[2].lower())
        return None
    result = _resolve_unqualified_column(
        scope, column.name, visible_by_scope_id, output_columns_by_scope_id, column_catalog,
    )
    if result[0] == _UNQUALIFIED_STATUS_OK and result[1] == "physical":
        return (result[2].lower(), result[3].lower())
    return None


def _confirmed_cross_object_pairs_in_scope(
    scope: Scope,
    visible_by_scope_id: dict[int, dict[str, tuple[str, object]]],
    output_columns_by_scope_id: dict[int, tuple[str, ...]],
    column_catalog: dict[str, dict[str, str] | None],
    confirmed_edges: frozenset,
) -> set[frozenset]:
    """
    {object_fqn_lower, object_fqn_lower} for every pair of DISTINCT
    physical objects this ONE scope's own equalities (every JOIN...ON
    clause belonging to this scope, plus this scope's own WHERE clause —
    never a child/nested scope's clauses, each of which is checked
    independently on its own turn) connect via a CONFIRMED column pair.

    Walking every exp.EQ found ANYWHERE inside each predicate (regardless
    of AND/OR nesting) is deliberately permissive about STRUCTURE — an
    extra, unrelated equality elsewhere in a compound ON/WHERE clause never
    prevents a genuine confirmed equality elsewhere in the same clause from
    counting — while remaining strict about TRUST: only an equality whose
    BOTH sides resolve to real physical columns, on DIFFERENT physical
    objects, matching a confirmed_relationship_edges pair, ever contributes
    an edge. `ON 1=1` (both sides Literal, never Column) and a same-object
    column comparison both contribute nothing, by construction.
    """
    predicate_exprs: list[exp.Expression] = []
    for join_node in scope.expression.args.get("joins") or []:
        on_expr = join_node.args.get("on")
        if on_expr is not None:
            predicate_exprs.append(on_expr)
    where_expr = scope.expression.args.get("where")
    if where_expr is not None:
        predicate_exprs.append(where_expr)

    connected: set[frozenset] = set()
    for predicate in predicate_exprs:
        for eq in predicate.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            left_ref = _resolve_equality_operand(left, scope, visible_by_scope_id, output_columns_by_scope_id, column_catalog)
            right_ref = _resolve_equality_operand(right, scope, visible_by_scope_id, output_columns_by_scope_id, column_catalog)
            if left_ref is None or right_ref is None or left_ref[0] == right_ref[0]:
                continue
            if frozenset({left_ref, right_ref}) in confirmed_edges:
                connected.add(frozenset({left_ref[0], right_ref[0]}))
    return connected


def _validate_joins(
    tree: exp.Expression,
    schema_context: SchemaContextResult,
    fqn_by_lower: dict[str, str],
    bare_name_index: dict[str, list[str]],
) -> tuple[str, ...]:
    """
    The Phase 7, Step 3A join-validation orchestrator. Only ever called
    after table- and column-level validation have already fully succeeded
    (mirroring _validate_columns's own precondition). Returns a tuple of
    rejection reasons — empty means every JOIN in the query, in every scope
    (main query, every CTE body, every subquery, every UNION/EXCEPT/
    INTERSECT branch — traverse_scope() already yields all of these, the
    exact same mechanism _validate_columns already relies on), is supported
    by a confirmed relationship.

    Per scope: the set of DISTINCT physical objects visible in that scope
    alone (never counting a CTE/derived-table alias, which is not a
    physical object) must form ONE connected component under the confirmed
    cross-object equalities found in that scope's own JOIN/WHERE clauses
    (see _confirmed_cross_object_pairs_in_scope) — a plain union-find over
    that small per-scope object set. Zero or one physical object needs no
    connectivity at all (a single-object query, or a query whose only
    "other" sources are derived tables/CTEs, is always accepted here — Part
    3, requirement 7). Two or more DISCONNECTED groups — including a
    same-named-but-unconfirmed column pair, an `ON 1=1`, a bare comma/cross
    join with no connecting WHERE equality, or a genuinely confirmed object
    pair joined on the WRONG column pair — is rejected, naming the
    disconnected group(s) so the reason is auditable without echoing any
    raw SQL, credential, or driver text.
    """
    column_catalog = _build_column_catalog(schema_context)
    confirmed_edges = _build_confirmed_column_edges(schema_context)

    try:
        scopes = list(traverse_scope(tree))
    except Exception as exc:  # noqa: BLE001 — mirrors _validate_columns's own defensive catch
        return (f"query scope could not be analyzed for join validation: {exc}",)

    visible_by_scope_id = {id(s): _resolve_visible_sources(s, fqn_by_lower, bare_name_index) for s in scopes}
    output_columns_by_scope_id = {id(s): _compute_output_columns(s) for s in scopes}

    reasons: list[str] = []
    for scope in scopes:
        if not isinstance(scope.expression, exp.Select):
            continue  # a SetOperation wrapper scope joins nothing of its own

        visible = visible_by_scope_id[id(scope)]
        # `ref` here is the CANONICAL-case fqn (as _resolve_visible_sources/
        # _physical_table_fqn resolve it from fqn_by_lower) — lowercase it
        # explicitly so it matches _resolve_equality_operand's own
        # (fqn_lower, col_lower) return shape used below; connected_pairs
        # and this union-find must share one consistent casing or every
        # lookup between them silently (or, worse, loudly via KeyError)
        # fails to match.
        physical_fqns_lower = sorted({ref.lower() for kind, ref in visible.values() if kind == "physical"})
        if len(physical_fqns_lower) <= 1:
            continue  # nothing to connect: 0-1 physical objects, or only derived-table sources

        connected_pairs = _confirmed_cross_object_pairs_in_scope(
            scope, visible_by_scope_id, output_columns_by_scope_id, column_catalog, confirmed_edges,
        )

        parent = {fqn: fqn for fqn in physical_fqns_lower}

        def _find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for pair in connected_pairs:
            a, b = tuple(pair)
            root_a, root_b = _find(a), _find(b)
            if root_a != root_b:
                parent[root_a] = root_b

        groups: dict[str, list[str]] = {}
        for fqn in physical_fqns_lower:
            groups.setdefault(_find(fqn), []).append(fqn)

        if len(groups) > 1:
            group_descriptions = sorted(
                "{" + ", ".join(fqn_by_lower.get(f, f) for f in sorted(group)) + "}"
                for group in groups.values()
            )
            reasons.append(
                "query joins physical object(s) with no confirmed relationship connecting them "
                f"(disconnected group(s): {'; '.join(group_descriptions)}) — every join between distinct "
                "physical objects must be supported by a confirmed relationship's own column pair, never "
                "a shared column name, an unconfirmed relationship, or a comma/cross join alone"
            )

    return tuple(reasons)


# ── Phase 7, Step 3B, Part 3 — deterministic intent-evidence validation ────
#
# the retired SQL-review stage already enforced the STRUCTURAL
# contract (bounded shapes, no blank/duplicate entries) and that every
# material requirement has SOME matching evidence entry — both pure
# self-consistency checks with no access to real schema metadata. This is
# the schema-TRUTH check that module deliberately cannot do: is each
# evidence entry's object_fqn/column_name actually real, actually used by
# THIS validated SQL, and — for anything other than a bare entity
# requirement — backed by a real column. This is the FINAL authority (per
# this module's own Phase 7, Step 3A, Part 3 precedent for JOIN safety): a
# reviewer that structurally satisfied its own contract but named a
# nonexistent column, or a column from an object the SQL never references,
# is still rejected here.


def _validate_intent_evidence(
    intent_evidence: ReviewedIntentEvidence,
    fqn_by_lower: dict[str, str],
    referenced_fqns: set[str],
    column_catalog: dict[str, dict[str, str] | None],
) -> list[str]:
    reasons: list[str] = []
    entity_requirements_lower = {e.lower() for e in intent_evidence.intent.entities}
    valid_requirements_lower: set[str] = set()

    for item in intent_evidence.evidence:
        canonical = fqn_by_lower.get(item.object_fqn.lower())
        if canonical is None:
            reasons.append(
                f"evidence for {item.requirement!r} references object {item.object_fqn!r} "
                "not present in the supplied schema context"
            )
            continue
        if canonical not in referenced_fqns:
            reasons.append(
                f"evidence for {item.requirement!r} references object {canonical!r} "
                "which the reviewed SQL does not actually use"
            )
            continue
        if item.column_name is None:
            if item.requirement.lower() not in entity_requirements_lower:
                reasons.append(
                    f"evidence for {item.requirement!r} must name a real supporting column — "
                    "only a bare entity/row-existence requirement may omit one"
                )
                continue
        else:
            columns = column_catalog.get(canonical)
            if not columns or item.column_name.lower() not in columns:
                reasons.append(
                    f"evidence for {item.requirement!r} references column {item.column_name!r} "
                    f"which does not exist on {canonical!r}"
                )
                continue
        valid_requirements_lower.add(item.requirement.lower())

    for requirement in material_requirements(intent_evidence.intent):
        if requirement.lower() not in valid_requirements_lower:
            reasons.append(f"material requirement {requirement!r} has no valid supporting evidence")

    return reasons


# ── Tier 3/4 governance re-check (Phase 7, Step 6E1) ────────────────────────
#
# An INDEPENDENT, final re-derivation — never trusts why an object reached
# schema_context (a frontier force-admission, a caller-selected connector,
# ...). Runs fresh from storage every single call (no cache trusted across
# a request), so a bug or future change anywhere earlier in the pipeline
# (schema_frontier, schema_relationship_completion, schema_exact_context)
# can never silently bypass it, and prompt/model behavior can never talk
# its way past it. Only ever runs when the caller supplies source_id/
# user_id (see validate_sql_candidate's own docstring) — every existing
# caller that omits them keeps validating exactly as before.

_GOVERNANCE_RESTRICTED_TIERS = frozenset({3, 4})


def _validate_governance_restricted_tier34_columns(
    schema_context: SchemaContextResult,
    resolved_fqns: set[str],
    validated_column_references: "tuple[ValidatedColumnReference, ...]",
    *,
    source_id: int,
    user_id: str,
) -> list[str]:
    """Any Tier 3/4 object (core.sqlservice.retrieval_documents.
    classify_retrieval_tier's own system/framework or backup/archive/
    staging tier) actually referenced by the validated SQL must carry its
    own owner-confirmed VERIFIED business-knowledge mapping (data.
    business_knowledge_mapping_store) — a confirmed relationship path
    alone (which is all core.sqlservice.schema_relationship_completion/
    schema_exact_context require of an already-SELECTED object) is never
    sufficient authorization here either. core.sqlservice.business_
    knowledge_mapper._verify_proposal already refuses to auto-verify any
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY object (every Tier 3/4 table,
    unconditionally), so a VERIFIED mapping for one can only exist as an
    explicit, manually authored authoritative decision — never AI
    proposal or model confidence. Every column of that object the SQL
    actually references must also be inside that mapping's own approved
    column set (or the mapping named no columns at all, in which case
    only its already-required join/identity columns are trusted, never
    an arbitrary attribute). Fails closed: no schema_fingerprint, no
    VERIFIED mapping, or any out-of-mapping column rejects the whole
    candidate."""
    doc_by_fqn_lower = {d.object_fqn.lower(): d for d in schema_context.documents}
    restricted_fqns = sorted(
        fqn for fqn in resolved_fqns
        if doc_by_fqn_lower.get(fqn.lower()) is not None
        and doc_by_fqn_lower[fqn.lower()].retrieval_tier in _GOVERNANCE_RESTRICTED_TIERS
    )
    if not restricted_fqns:
        return []

    schema_fingerprint = schema_context.schema_fingerprint
    if not schema_fingerprint:
        return [
            f"tier 3/4 object(s) {restricted_fqns} referenced but no schema_fingerprint is available "
            "to verify governance approval -- rejected fail-closed"
        ]

    evidence_result = load_business_knowledge_evidence(source_id, user_id, schema_fingerprint)
    evidence_by_fqn_lower = evidence_result.mappings_by_object_fqn_lower if evidence_result is not None else {}

    referenced_columns_by_object: dict[str, set[str]] = {}
    for ref in validated_column_references:
        referenced_columns_by_object.setdefault(ref.object_fqn.lower(), set()).add(ref.column_name.lower())

    reasons: list[str] = []
    for fqn in restricted_fqns:
        key = fqn.lower()
        verified = [m for m in evidence_by_fqn_lower.get(key, ()) if m.mapping_status == "VERIFIED"]
        if not verified:
            reasons.append(
                f"tier 3/4 object {fqn!r} has no VERIFIED business-knowledge governance mapping -- "
                "a confirmed relationship alone is never sufficient authorization"
            )
            continue
        # An empty approved-column set (the mapping named no specific
        # columns) approves the object's structural join/identity columns
        # ONLY -- it must still reject any other column reference, exactly
        # like an explicit column list would (see the mirrored comment in
        # core.sqlservice.schema_frontier's own governance force-admit).
        # Structural columns (this object's own primary key, and its own
        # side of any confirmed_relationship_edges join column -- the SAME
        # metadata _validate_joins above already trusts for join safety)
        # are always allowed regardless of the mapping's own column list --
        # a JOIN cannot function at all without them, and they are row
        # identity/connectivity, never a selectable business attribute.
        doc = doc_by_fqn_lower[key]
        structural_columns = {
            str(c).strip().lower() for c in (doc.metadata.get("primary_key_columns") or ())
        } | {
            str(edge.get("column") or "").strip().lower()
            for edge in (doc.metadata.get("confirmed_relationship_edges") or ())
            if str(edge.get("column") or "").strip() not in ("", "*")
        }
        approved_columns = {c.lower() for m in verified for c in m.columns} | structural_columns
        unapproved = sorted(referenced_columns_by_object.get(key, set()) - approved_columns)
        if unapproved:
            reasons.append(
                f"tier 3/4 object {fqn!r} references column(s) {unapproved} not in its approved "
                "governance mapping's column set"
            )
    return reasons


# ── Secret/credential column rejection (Phase 7, Step 6E3B, Parts 6/8) ─────
#
# Unconditional -- runs for EVERY validated candidate regardless of
# source_id/user_id or object tier (unlike the Tier-3/4 governance
# re-check above, which only runs when both are supplied). core.
# dictionary.pii_detector.is_restricted_category() is the SAME signal
# core.governance.pii_access_policy already treats as an unconditional
# BLOCK for every requester -- this enforces that same policy here so a
# generated SQL candidate that selects a password/token/connection-string/
# encryption-key/... column is rejected before it can ever be executed,
# never only masked after the fact. Ordinary PII (names, emails, DOB,
# SSN, passport, financial/contact fields, ...) is deliberately NOT
# checked here -- per the owner's policy, that stays fully selectable for
# an authorized source user; only the narrower is_restricted_category()
# set (actual system-access secrets) is ever rejected.


def _validate_secret_credential_columns(
    validated_column_references: "tuple[ValidatedColumnReference, ...]",
) -> list[str]:
    from core.dictionary.pii_detector import is_restricted_category

    reasons: list[str] = []
    seen: set[str] = set()
    for ref in validated_column_references:
        key = f"{ref.object_fqn.lower()}.{ref.column_name.lower()}"
        if key in seen or not is_restricted_category(ref.column_name):
            continue
        seen.add(key)
        reasons.append(
            f"column {ref.column_name!r} on {ref.object_fqn!r} is a restricted authentication/credential "
            "secret column -- never selectable regardless of authorization"
        )
    return reasons


# ── View suitability validation (Phase 7, Step 6E3B, Part 8) ───────────────
#
# Deterministic, reads the persisted ViewSuitabilityProfile (core.
# sqlservice.view_profiler / data.view_profile_service, Part 5) for each
# resolved VIEW object -- never relies only on the generation model's own
# explanation or confidence. A view with no persisted profile yet (not
# profiled this connect, or profiling produced no row) is never rejected
# BY THIS CHECK on that basis alone -- suitability is additive evidence on
# top of every existing fabricated-object/column/join check above, never a
# new blanket gate that would reject an otherwise-valid, unprofiled view.


def _count_star_or_constant(node: exp.Expression) -> bool:
    if not isinstance(node, exp.Count):
        return False
    arg = node.this
    return arg is None or isinstance(arg, exp.Star) or isinstance(arg, exp.Literal)


def _count_distinct_column_names(tree: exp.Expression) -> set[str]:
    names: set[str] = set()
    for node in tree.find_all(exp.Count):
        arg = node.this
        is_distinct = bool(node.args.get("distinct")) or isinstance(arg, exp.Distinct)
        if not is_distinct:
            continue
        for col in (arg.find_all(exp.Column) if arg is not None else ()):
            names.add(col.name.lower())
    return names


def _validate_view_suitability(
    tree: exp.Expression, schema_context: SchemaContextResult, resolved_fqns: "set[str]",
    *, source_id: int | None, requested_population_filters: "tuple[str, ...]" = (),
) -> list[str]:
    if source_id is None:
        return []
    doc_by_fqn_lower = {d.object_fqn.lower(): d for d in schema_context.documents}
    view_fqns = sorted(
        fqn for fqn in resolved_fqns
        if doc_by_fqn_lower.get(fqn.lower()) is not None and doc_by_fqn_lower[fqn.lower()].object_type == "VIEW"
    )
    if not view_fqns:
        return []

    from data.view_profile_service import get_view_profiles_by_fqns, get_current_definition_hashes
    from core.sqlservice.view_profiler import (
        classify_profile_state, PROFILE_STATE_COMPLETE, PROFILE_STATE_LIMITED_COMPLETE, PURPOSE_AGGREGATION,
    )
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION

    try:
        profiles = get_view_profiles_by_fqns(source_id, view_fqns)
    except Exception:  # noqa: BLE001 -- profiling is best-effort evidence, never fatal to validation itself
        profiles = {}
    try:
        current_definition_hashes = get_current_definition_hashes(source_id, view_fqns)
    except Exception:  # noqa: BLE001
        current_definition_hashes = {}

    reasons: list[str] = []
    has_group_by_or_aggregate = bool(tree.args.get("group")) or any(True for _ in tree.find_all(exp.AggFunc))
    has_naive_count = any(_count_star_or_constant(n) for n in tree.find_all(exp.Count))
    distinct_counted = _count_distinct_column_names(tree)
    has_join = bool(tree.args.get("joins"))

    for fqn in view_fqns:
        profile = profiles.get(fqn.lower())
        # Phase 7, Step 6E3B2, Part 1 — fail CLOSED, UNCONDITIONALLY: a
        # view may be referenced by generated SQL ONLY when its profile is
        # COMPLETE, build/schema-fingerprint matched, definition-hash
        # matched, profiler-contract-version matched, AND current under
        # the active secret/PII policy. Every other state (LINEAGE_
        # INCOMPLETE/TYPED_FAILURE/MISSING/STALE) blocks execution for
        # EVERY query shape -- a plain row-detail query is no longer
        # exempted just because it has no aggregation (this tightens
        # Step 6E3B1 Part 2's "detail queries may still proceed" rule,
        # which is superseded here). The view stays visible in retrieval/
        # diagnostic evidence (schema_frontier/fast_path_context render it
        # regardless) -- only EXECUTION is blocked at this, the one point
        # both the fast path and the legacy path actually call.
        state = classify_profile_state(
            profile, schema_fingerprint=schema_context.schema_fingerprint,
            current_definition_hash=current_definition_hashes.get(fqn.lower()),
            current_secret_policy_version=SECRET_POLICY_VERSION,
        )
        # Phase 7, Step 6E3B3, Part 5 -- PROFILE_STATE_LIMITED_COMPLETE is
        # now ALSO accepted here, strictly narrower-trust than COMPLETE
        # (never a blanket "safe for anything" verdict the way COMPLETE
        # is -- see view_profiler.classify_profile_state's own updated
        # docstring). Every restriction below (suitability, naive-COUNT,
        # aggregation-grain, join, secret-column) applies IDENTICALLY to a
        # LIMITED_COMPLETE view -- it gets no weaker enforcement than a
        # COMPLETE one, only a weaker EVIDENCE basis for having reached
        # those same suitability facts. PROFILE_STATE_UNSAFE/TYPED_
        # FAILURE/LINEAGE_INCOMPLETE/MISSING/STALE still block entirely.
        if state not in (PROFILE_STATE_COMPLETE, PROFILE_STATE_LIMITED_COMPLETE) or profile.suitability is None:
            reasons.append(
                f"view {fqn!r} has no currently-trusted profile (state={state}) -- generated SQL may not "
                "reference it until profiling completes under the current build/definition/secret-policy "
                "identity; ask a more specific question or wait for profiling to complete"
            )
            continue
        suitability = profile.suitability

        if profile.secret_columns:
            reasons.append(
                f"view {fqn!r} exposes restricted secret/credential column(s) {list(profile.secret_columns)} "
                "-- never selectable via this view"
            )
        if not has_group_by_or_aggregate and not suitability.suitable_for_detail:
            reasons.append(
                f"view {fqn!r} is not suitable for a row-detail query ({suitability.unsuitable_reasons})"
            )
        if has_naive_count and profile.required_distinct_key and profile.required_distinct_key.lower() not in distinct_counted:
            reasons.append(
                f"view {fqn!r} has row-duplication risk ({profile.duplication_risk}); COUNT(*)/COUNT(1) "
                f"would overcount -- use COUNT(DISTINCT {profile.required_distinct_key}) instead"
            )
        if has_group_by_or_aggregate and not suitability.suitable_for_aggregation:
            # Step 6E3B2, Part 5 (found via real-data pre-backfill
            # simulation, not a synthetic fixture) -- suitable_for_
            # aggregation is a single blanket flag that goes False for
            # ANY fan-out/row-preservation risk, with no awareness that
            # the SAME query may already have neutralized exactly that
            # risk via COUNT(DISTINCT required_distinct_key) -- the
            # has_naive_count check just above already proves that. Without
            # this carve-out, a view's own correct, recommended query shape
            # (COUNT(DISTINCT key)) would be rejected right alongside the
            # naive COUNT(*) it exists to replace, making any LIKELY_FANOUT
            # view uncountable under any phrasing -- defeating the whole
            # point of required_distinct_key. Only suppressed when the
            # aggregation-unsuitability's SOLE driver is that fan-out
            # reason and the query demonstrably guards against it; any
            # OTHER aggregation-incompatibility reason (e.g. incompatible
            # grain) still blocks unconditionally.
            aggregation_reasons = [r for r in suitability.unsuitable_reasons if r.startswith(f"{PURPOSE_AGGREGATION}:")]
            fanout_already_guarded = (
                bool(profile.required_distinct_key)
                and profile.required_distinct_key.lower() in distinct_counted
                and aggregation_reasons == [f"{PURPOSE_AGGREGATION}:row_preservation_or_fanout_risk"]
            )
            if not fanout_already_guarded:
                reasons.append(
                    f"view {fqn!r} is not suitable for aggregation ({suitability.unsuitable_reasons})"
                )
        if has_join and not suitability.suitable_for_join:
            reasons.append(
                f"view {fqn!r} is not suitable as a JOIN source/target ({suitability.unsuitable_reasons})"
            )

        # Phase 7, Step 6E3B1, Part 6 — deterministic restricted-population
        # enforcement. core.sqlservice.schema_selection_service/schema_
        # selection_verifier already established this EXACT contract
        # (`unrestricted_request = not intent_summary.filters`, reject a
        # RESTRICTED_VIEW selection when no filter was declared) for the
        # legacy/plain selection pipeline -- but neither of those functions
        # is ever called by the active AI-first fast path (core.sqlservice.
        # ai_first_fast_path_service._selection_adapter only reshapes the
        # candidate for ambiguity_guard/reporting, never runs an authority
        # check on it). This module runs on BOTH paths (validate_sql_
        # candidate is the one shared, final safety authority), so this is
        # the single generic point that actually closes that gap for both.
        # Keyed only on the view's own already-persisted `population_
        # restrictions` evidence and the requester's own declared `filters`
        # -- never a view name, never a hardcoded question list, never
        # model confidence (that field is never read here at all).
        if profile.population_restrictions and not requested_population_filters:
            reasons.append(
                f"view {fqn!r} has a population restriction ({profile.population_restrictions[0]!r}) but the "
                "request declared no filter/scope -- an unrestricted \"all\" question may not use a "
                "population-restricted view; ask a more specific, scoped question"
            )

    return reasons


# ── Public entry point ────────────────────────────────────────────────────


def validate_sql_candidate(
    generation_result: SqlGenerationResult,
    schema_context: SchemaContextResult,
    *,
    max_sql_characters: int = SQL_VALIDATION_MAX_SQL_CHARACTERS,
    max_ast_nodes: int = SQL_VALIDATION_MAX_AST_NODES,
    max_ctes: int = SQL_VALIDATION_MAX_CTES,
    max_joins: int = SQL_VALIDATION_MAX_JOINS,
    max_referenced_objects: int = SQL_VALIDATION_MAX_REFERENCED_OBJECTS,
    max_column_references: int = SQL_VALIDATION_MAX_COLUMN_REFERENCES,
    intent_evidence: ReviewedIntentEvidence | None = None,
    source_id: int | None = None,
    user_id: str | None = None,
    requested_population_filters: tuple[str, ...] = (),
) -> SqlValidationResult:
    """
    Validate one SqlGenerationResult's candidate SQL against
    schema_context, using a real T-SQL AST (never regex-only matching).

    Never executes the SQL, never modifies it, never attempts automatic
    repair — a rejected candidate is reported with every reason found
    (STATUS_REJECTED, `error` joining all of them), not just the first.

    Order of operations:
      1. Validate limit configuration and that `generation_result` and
         `schema_context` actually describe the same build (raises
         ValueError — a caller/programmer contract violation, never a
         routine condition).
      2. Require generation_result.status == OK, a non-None candidate that
         does not need clarification, and schema_context.status == OK —
         anything else short-circuits with STATUS_INVALID_CANDIDATE (there
         is simply no SQL to validate).
      3. Reject SQL exceeding max_sql_characters, or containing any SQL
         comment marker (comments could hide a blocked keyword from
         reliable validation — rejected outright, never stripped).
      4. Parse with sqlglot(dialect="tsql"); a parse failure or anything
         other than exactly one statement is rejected.
      5. Reject if the AST's total node count exceeds max_ast_nodes.
      6. Require the root statement to be a SELECT or a read-only set
         operation (UNION/UNION ALL/EXCEPT/INTERSECT).
      7. Walk the ENTIRE tree (not just the root) for any forbidden
         statement type, a banned rowset function (OPENROWSET/OPENQUERY/
         OPENDATASOURCE), or a SELECT INTO — catches a write hidden inside
         a CTE body, which sqlglot's grammar parses more permissively than
         real SQL Server syntax would ever allow.
      8. Enforce max_ctes/max_joins.
      9. Resolve every real table/view reference against schema_context
         (case-insensitive; unique unqualified names permitted, ambiguous
         ones rejected; cross-database/linked-server/temp-table/table-
         variable references always rejected regardless of name).
      10. Cross-check the resolved set against the candidate's own
          declared referenced_object_fqns in both directions (undeclared
          SQL references, and declared-but-unused ones) — reject on any
          mismatch. Return the canonical context spellings, never the
          SQL's or the candidate's own casing.
      11. Step 10 addition — column-level validation (see _validate_columns
          and this module's own "Column-level validation" section above):
          every physical table/view actually referenced must have
          trustworthy structured column metadata (fail closed otherwise);
          every column reference anywhere in the query (including CTEs,
          subqueries, UNION branches, window/aggregate/CASE expressions,
          and wildcards) must resolve to a real column on its qualified
          alias, or uniquely within its local scope if unqualified.
          Returns the deduplicated, deterministically ordered
          ValidatedColumnReference tuple.
      12. Phase 7, Step 3A, Part 3 addition — deterministic confirmed-
          relationship JOIN validation (see _validate_joins above): every
          JOIN between two DISTINCT PHYSICAL objects, in every scope
          (main query, every CTE/subquery, every UNION/EXCEPT/INTERSECT
          branch), must be backed by a confirmed relationship's own column
          pair (core.sqlservice.retrieval_documents' confirmed_relationship_
          edges metadata) — never a shared column name alone, an object-
          level-only mention, a document-text/prose reference, `ON 1=1`, or
          a comma/cross join with no connecting confirmed equality. This is
          the FINAL authority on join safety: AI review approval can never
          override a rejection here.
      13. Phase 7, Step 3B, Part 3 addition — when `intent_evidence` is
          supplied (the reviewer's own structurally-valid claim set; see
          the retired SQL-review stage and core.sqlservice.
          intent_evidence), deterministically re-verify every evidence
          entry against THIS candidate's own resolved objects/columns (see
          _validate_intent_evidence above) and reject if any entry is
          untrue or any material requirement ends up uncovered by valid
          evidence. `intent_evidence=None` (the default) skips this step
          entirely — every existing caller that never passes it keeps
          validating exactly as before.
      14. Phase 7, Step 6E1 addition — when both `source_id` and `user_id`
          are supplied, independently re-verify (see
          _validate_governance_restricted_tier34_columns above) that every
          Tier 3/4 object the SQL actually references carries its own
          VERIFIED business-knowledge governance mapping, and that every
          column referenced on it is within that mapping's approved set
          (plus its own structural join/identity columns). This is the
          FINAL authority on Tier 3/4 access, independent of and never
          trusting whatever reasoning admitted the object earlier in the
          pipeline — a rejection here can never be overridden.
          `source_id=None`/`user_id=None` (the default) skips this step
          entirely — every existing caller that never passes them keeps
          validating exactly as before.
    """
    for name, value in (
        ("max_sql_characters", max_sql_characters), ("max_ast_nodes", max_ast_nodes),
        ("max_ctes", max_ctes), ("max_joins", max_joins),
        ("max_referenced_objects", max_referenced_objects), ("max_column_references", max_column_references),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")

    if generation_result.build_id != schema_context.build_id:
        raise ValueError(
            f"generation_result.build_id={generation_result.build_id!r} does not match "
            f"schema_context.build_id={schema_context.build_id!r}"
        )

    sql = generation_result.candidate.sql if generation_result.candidate is not None else ""

    if generation_result.status != _SQL_GENERATION_STATUS_OK or generation_result.candidate is None:
        return _invalid_candidate_result(
            generation_result, sql, f"generation_result.status={generation_result.status!r} is not OK"
        )
    if generation_result.candidate.needs_clarification or not generation_result.candidate.sql.strip():
        return _invalid_candidate_result(
            generation_result, sql, "candidate requires clarification; there is no SQL to validate"
        )
    if schema_context.status != _SCHEMA_CONTEXT_STATUS_OK:
        return _invalid_candidate_result(
            generation_result, sql, f"schema_context.status={schema_context.status!r} is not OK"
        )

    tree, shape_reason = parse_single_read_only_statement(
        sql, max_sql_characters=max_sql_characters, max_ast_nodes=max_ast_nodes,
    )
    if tree is None:
        return _rejected_result(generation_result, sql, [shape_reason])

    reasons: list[str] = list(find_forbidden_construct_reasons(tree))

    cte_count = sum(1 for _ in tree.find_all(exp.CTE))
    if cte_count > max_ctes:
        reasons.append(f"CTE count {cte_count} exceeds the maximum of {max_ctes}")

    join_count = sum(1 for _ in tree.find_all(exp.Join))
    if join_count > max_joins:
        reasons.append(f"JOIN count {join_count} exceeds the maximum of {max_joins}")

    if reasons:
        return _rejected_result(generation_result, sql, reasons)

    cte_names_lower = {cte.alias.lower() for cte in tree.find_all(exp.CTE) if cte.alias}
    fqn_by_lower, bare_name_index = _build_object_index(schema_context)
    resolved_fqns, resolution_reasons = _resolve_table_references(
        tree, cte_names_lower, fqn_by_lower, bare_name_index,
    )
    if resolution_reasons:
        return _rejected_result(generation_result, sql, resolution_reasons)

    if len(resolved_fqns) > max_referenced_objects:
        return _rejected_result(
            generation_result, sql,
            [f"referenced-object count {len(resolved_fqns)} exceeds the maximum of {max_referenced_objects}"],
        )

    declared_canonical: set[str] = set()
    for declared in generation_result.candidate.referenced_object_fqns:
        canonical = fqn_by_lower.get(declared.lower())
        if canonical is None:
            return _rejected_result(
                generation_result, sql,
                [f"declared referenced object {declared!r} is not present in the supplied schema context"],
            )
        declared_canonical.add(canonical)

    undeclared = sorted(resolved_fqns - declared_canonical)
    if undeclared:
        return _rejected_result(
            generation_result, sql,
            [f"SQL references object(s) not declared in referenced_object_fqns: {undeclared}"],
        )
    unused = sorted(declared_canonical - resolved_fqns)
    if unused:
        return _rejected_result(
            generation_result, sql,
            [f"referenced_object_fqns declares object(s) not used anywhere in the SQL: {unused}"],
        )

    validated_column_references, column_reasons = _validate_columns(
        tree, resolved_fqns, fqn_by_lower, bare_name_index, schema_context, max_column_references,
    )
    if column_reasons:
        return _rejected_result(generation_result, sql, list(column_reasons))

    secret_reasons = _validate_secret_credential_columns(validated_column_references)
    if secret_reasons:
        return _rejected_result(generation_result, sql, secret_reasons)

    join_reasons = _validate_joins(tree, schema_context, fqn_by_lower, bare_name_index)
    if join_reasons:
        return _rejected_result(generation_result, sql, list(join_reasons))

    if source_id is not None and user_id is not None:
        governance_reasons = _validate_governance_restricted_tier34_columns(
            schema_context, resolved_fqns, validated_column_references,
            source_id=source_id, user_id=user_id,
        )
        if governance_reasons:
            return _rejected_result(generation_result, sql, governance_reasons)

    suitability_reasons = _validate_view_suitability(
        tree, schema_context, resolved_fqns, source_id=source_id,
        requested_population_filters=requested_population_filters,
    )
    if suitability_reasons:
        return _rejected_result(generation_result, sql, suitability_reasons)

    if intent_evidence is not None:
        column_catalog = _build_column_catalog(schema_context)
        evidence_reasons = _validate_intent_evidence(intent_evidence, fqn_by_lower, resolved_fqns, column_catalog)
        if evidence_reasons:
            return _rejected_result(generation_result, sql, evidence_reasons)

    return SqlValidationResult(
        status=STATUS_OK,
        sql=sql,
        canonical_referenced_object_fqns=tuple(sorted(resolved_fqns)),
        statement_type=type(tree).__name__,
        build_id=generation_result.build_id,
        schema_fingerprint=generation_result.schema_fingerprint,
        warnings=generation_result.warnings,
        error=None,
        validated_column_references=validated_column_references,
    )
