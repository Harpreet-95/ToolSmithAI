"""
Automatic schema-object authority/scope classification — SQL rebuild
Phase 7, Step 3E.

Verified root cause (Phase 7, Step 3D1 read-only equivalence audit): Gate A
already proves every required object is present in the AI schema-selection
frontier for all answerable cases, yet the selector still picks the wrong
object in most of those cases — not because retrieval is too narrow, but
because the compact candidate summary it is shown carries no FACTS that
would let it tell a canonical business table apart from a narrowly filtered
reporting view, a pre-aggregated view with an incompatible grain, or a view
built on an unrelated/Tier-3/4 object. This module supplies exactly those
facts, computed ONCE per build from metadata this codebase ALREADY persists
(never a new live probe, never regex-only text matching, never a per-
question AI/embedding call):

  - core.sqlservice.retrieval_documents' own retrieval_tier/column
    metadata (already loaded as `documents` by every caller of this
    module — the same IndexedSchemaDocument-shaped sequence
    core.sqlservice.canonical_scoring/business_concept_index already
    consume).
  - `semantic_entity_contracts` (data.semantic_contract_service /
    data.query_planning_service's own persisted, human-independent
    entity-grounding table) for `canonical_table_fqn`/`preferred_view_fqn`
    per business entity — read directly here (read-only), never through
    that module's much larger legacy-orchestrator-facing API surface.
  - `view_semantic_evidence` (data.view_semantics_service) for each view's
    OWN stored `definition_sql` and catalog-cross-checked
    `referenced_tables_json` — the "stored catalog dependency" identity
    source of truth this module NEVER second-guesses or re-derives (see
    _view_dependency_closure's own docstring: AST analysis below only ever
    SUPPLEMENTS this list with join-kind/filter/grain facts about the
    dependencies it already names; it never invents a new one).

AST derivation (sqlglot, dialect="tsql" — the SAME parser/dialect
core.sqlservice.sql_validation already uses for read-only-query validation;
never regex-only matching, per this step's own explicit requirement) reads
each view's stored `definition_sql` text to determine, at the OUTER query
level only (never conflating a nested subquery's own aggregation/filtering
with the view's own overall row grain — see _root_level_aggregate_functions
and _prune_at_nested_query's own docstrings): WHERE/HAVING presence and
whether any comparison there is against a hardcoded literal, GROUP BY,
root-level aggregate functions, DISTINCT, TOP/LIMIT, UNION/set operations,
each FROM/JOIN table's join kind (LEFT/INNER/RIGHT/FULL/CROSS), and any
projected column that is actually a hardcoded literal masquerading as a
real business column (e.g. `'Some Program' AS Program`) — the exact,
generalized version of the Step 3D1 audit's own hand-inspection finding.

Scope: pure classification. This module never calls OpenAI, never opens a
live SQL Server connection, never executes anything, never writes to the
database, and never approves/persists anything — `semantic_entity_contracts`
and `view_semantic_evidence` are read-only inputs here, written by their own
existing modules elsewhere. No table/view name from any specific customer
schema appears anywhere in this file (see
tests/test_sqlservice_schema_object_authority.py's own
test_no_domain_specific_tokens_in_production_module guard, mirroring
core.sqlservice.canonical_scoring's identical guarantee).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Mapping, Sequence

import sqlglot
from sqlglot import exp

from data.db import get_connection

__all__ = [
    "AUTHORITY_CONTRACT_VERSION",
    "AUTHORITY_CLASS_CANONICAL_TABLE",
    "AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW",
    "AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW",
    "AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW",
    "AUTHORITY_CLASS_RESTRICTED_VIEW",
    "AUTHORITY_CLASS_INCOMPATIBLE_GRAIN",
    "AUTHORITY_CLASS_UNSAFE_DEPENDENCY",
    "AUTHORITY_CLASS_UNKNOWN",
    "AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW",
    "AUTHORITY_CLASS_LINEAGE_INCOMPLETE",
    "classify_framework_backed_eligibility",
    "RESTRICTION_STATUS_NONE",
    "RESTRICTION_STATUS_FILTERED",
    "RESTRICTION_STATUS_UNKNOWN",
    "GRAIN_CLASS_BASE_ROW",
    "GRAIN_CLASS_AGGREGATED",
    "GRAIN_CLASS_DEDUPLICATED",
    "GRAIN_CLASS_UNKNOWN",
    "ROW_PRESERVATION_PRESERVED",
    "ROW_PRESERVATION_AT_RISK",
    "ROW_PRESERVATION_REDUCED",
    "ROW_PRESERVATION_UNKNOWN",
    "UNSAFE_RETRIEVAL_TIERS",
    "SchemaObjectAuthority",
    "SchemaObjectAuthorityIdentity",
    "SchemaObjectAuthorityResult",
    "compute_schema_object_authority",
    "load_schema_object_authority",
    "clear_schema_object_authority_cache",
    "schema_object_authority_cache_size",
]

# ── Public classification vocabulary ───────────────────────────────────────

AUTHORITY_CLASS_CANONICAL_TABLE = "CANONICAL_TABLE"
AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW = "DECLARED_PREFERRED_VIEW"
AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW = "PROVEN_EQUIVALENT_VIEW"
AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW = "CONDITIONALLY_COMPATIBLE_VIEW"
AUTHORITY_CLASS_RESTRICTED_VIEW = "RESTRICTED_VIEW"
AUTHORITY_CLASS_INCOMPATIBLE_GRAIN = "INCOMPATIBLE_GRAIN"
AUTHORITY_CLASS_UNSAFE_DEPENDENCY = "UNSAFE_DEPENDENCY"
AUTHORITY_CLASS_UNKNOWN = "UNKNOWN"

# Phase 7, Step 6E3B, Part 5 -- two additional evidence-backed classes,
# layered ON TOP of (never a replacement for) the classes above. Neither
# constant is ever returned by _classify_one/compute_schema_object_
# authority itself -- every EXISTING caller (core.sqlservice.sql_
# validation's Tier-3/4 governance re-check, core.sqlservice.business_
# knowledge_mapper's UNSAFE_DEPENDENCY auto-verify refusal, ...) keeps
# seeing exactly the same AUTHORITY_CLASS_UNSAFE_DEPENDENCY it always has
# for a Tier-3/4-dependent view, so none of that existing enforcement
# changes behavior. These two classes are a DOWNSTREAM REFINEMENT computed
# by core.sqlservice.view_profiler / data.view_profile_service (which also
# has the output-column lineage this module does not compute), via
# classify_framework_backed_eligibility() below, and persisted only onto
# that module's own ViewProfile.authority_class -- a distinct, narrower-
# trust field the new view-suitability validation (Part 8) reads, never a
# mutation of this module's own SchemaObjectAuthority.authority_class.
AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW = "FRAMEWORK_BACKED_BUSINESS_VIEW"
AUTHORITY_CLASS_LINEAGE_INCOMPLETE = "LINEAGE_INCOMPLETE"

RESTRICTION_STATUS_NONE = "NONE"
RESTRICTION_STATUS_FILTERED = "FILTERED"
RESTRICTION_STATUS_UNKNOWN = "UNKNOWN"

GRAIN_CLASS_BASE_ROW = "BASE_ROW"
GRAIN_CLASS_AGGREGATED = "AGGREGATED"
GRAIN_CLASS_DEDUPLICATED = "DEDUPLICATED"
GRAIN_CLASS_UNKNOWN = "UNKNOWN"

ROW_PRESERVATION_PRESERVED = "PRESERVED"
ROW_PRESERVATION_AT_RISK = "AT_RISK"
ROW_PRESERVATION_REDUCED = "REDUCED"
ROW_PRESERVATION_UNKNOWN = "UNKNOWN"

# Retrieval tiers this module treats as structurally unsafe for
# authoritative substitution (core.sqlservice.retrieval_documents.
# classify_retrieval_tier's own Tier 3 = system/framework, Tier 4 =
# backup/archive/history/staging/temp/test/deprecated) — reused by FQN
# lookup against each object's OWN already-computed retrieval_tier, never
# re-derived from its name here.
UNSAFE_RETRIEVAL_TIERS = frozenset({3, 4})

# Bump whenever this module's CLASSIFICATION LOGIC changes in a way that
# could change a prior result for unchanged inputs — invalidates every
# cached SchemaObjectAuthorityResult even though the underlying schema/
# contract/view-definition state fingerprint (see _authority_state) is
# unchanged. Never bump for a pure refactor with identical behavior.
AUTHORITY_CONTRACT_VERSION = 1

# Bounded output sizes — this module's own results are rendered into an AI
# prompt (core.sqlservice.schema_frontier), so every list-shaped field stays
# small and deterministic, mirroring every other bounded field that module
# already enforces.
_MAX_DEPENDENCY_FQNS = 20
_MAX_UNSAFE_DEPENDENCY_FQNS = 10
_MAX_EVIDENCE_CODES = 10
_MAX_WARNINGS = 5
_MAX_CANONICAL_TABLE_FQNS = 5
_MAX_PREFERRED_VIEW_ENTITIES = 5
_MAX_VIEW_CLOSURE_DEPTH = 6

TSQL_DIALECT = "tsql"


# ── Public data model ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SchemaObjectAuthority:
    """One object's immutable authority/scope classification for one build.

    Every list-shaped field is a bounded, deterministically ordered tuple —
    see the `_MAX_*` constants above. `evidence` is a bounded tuple of
    short, stable evidence CODES (e.g. "where_literal_filter",
    "inner_join_unverified:dbo.SomeLookupTable") — never free-form prose,
    never raw SQL text (see this module's own docstring: full view SQL is
    never exposed past this module).
    """

    object_fqn: str
    object_type: str
    canonical_table_fqns: tuple[str, ...]
    preferred_view_for_entities: tuple[str, ...]
    authority_class: str
    restriction_status: str
    restrictions: tuple[str, ...]
    grain_class: str
    aggregation_status: str
    row_preservation_status: str
    dependency_fqns: tuple[str, ...]
    unsafe_dependency_fqns: tuple[str, ...]
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SchemaObjectAuthorityIdentity:
    """Exact cache identity (Phase 7, Step 3E, Part 4). `authority_state` is
    an opaque, comparison-only tuple (see _authority_state) that changes
    whenever semantic-contract or view-definition/dependency state changes
    for this source — two identities differing only in that field are never
    considered the same result. `build_id` alone already captures a schema
    change (a new schema snapshot always gets a new build_id — see every
    other *Identity in this package for the same convention), so no
    separate schema_fingerprint field is needed for THAT invalidation axis;
    it is still carried here, purely for observability/cross-checking, not
    as an independent cache key component."""

    build_id: int
    source_id: int
    schema_fingerprint: str | None
    authority_state: tuple
    contract_version: int = AUTHORITY_CONTRACT_VERSION


@dataclass(frozen=True)
class SchemaObjectAuthorityResult:
    """Result of one compute/load_schema_object_authority() call — an
    immutable, ready-to-use authority pass over one build's full document
    set (mirrors core.sqlservice.canonical_scoring.CanonicalScoringResult's
    own "every document gets a result, tier filtering stays the caller's
    job" contract)."""

    identity: SchemaObjectAuthorityIdentity
    authorities_by_fqn_lower: Mapping[str, SchemaObjectAuthority]
    document_count: int
    warnings: tuple[str, ...] = ()


# ── Cache state (semantic-contract/enrichment/view-definition state,
#    invalidation only — never question content, never credentials) ───────


def _table_state(conn: sqlite3.Connection, sql: str, params: tuple) -> tuple[int, str]:
    try:
        row = conn.execute(sql, params).fetchone()
        return (int(row["c"] or 0), str(row["m"] or ""))
    except Exception:  # noqa: BLE001 — a missing/not-yet-migrated table must never break retrieval
        return (0, "")


def _authority_state(source_id: int) -> tuple:
    """Cheap, indexed (count, latest-timestamp) signature per trust source
    this module reads — comparison-only, never treated as semantic evidence
    itself (same idiom as core.sqlservice.business_concept_index's own
    _enrichment_state, scoped to only the two tables this module actually
    consumes). Fails open per-table, so a partially-migrated/empty database
    still produces a stable, cacheable identity."""
    conn = get_connection()
    try:
        return (
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM semantic_entity_contracts WHERE source_id = ? AND resolution_status = 'RESOLVED'",
                (source_id,),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(discovered_at), '') AS m "
                "FROM view_semantic_evidence WHERE source_id = ?",
                (source_id,),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM sql_view_semantic_enrichments WHERE source_id = ? AND status = 'ACTIVE'",
                (source_id,),
            ),
        )
    finally:
        conn.close()


# ── Read-only trust-source loaders ──────────────────────────────────────────


def _load_semantic_contracts(source_id: int) -> list[dict]:
    """The latest RESOLVED contract per entity_name for this source — a
    direct, minimal read (never data.semantic_contract_service's own much
    larger legacy-orchestrator-facing API), matching that module's own
    get_latest_entity_contract's exact trust bar (latest contract_version,
    resolution_status == 'RESOLVED')."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT entity_name, canonical_table_fqn, preferred_view_fqn, resolution_status, contract_version "
            "FROM semantic_entity_contracts WHERE source_id = ? ORDER BY entity_name, contract_version DESC",
            (source_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — fail open: no contracts is a normal, valid state
        return []
    finally:
        conn.close()

    latest_by_entity: dict[str, dict] = {}
    for row in rows:
        d = dict(row)
        if d["entity_name"] not in latest_by_entity:
            latest_by_entity[d["entity_name"]] = d
    return [d for d in latest_by_entity.values() if d.get("resolution_status") == "RESOLVED"]


@dataclass(frozen=True)
class _ViewEvidenceRow:
    definition_sql: str | None
    referenced_tables: tuple[str, ...]


def _load_view_semantic_evidence(source_id: int) -> dict[str, _ViewEvidenceRow]:
    """object_fqn_lower -> _ViewEvidenceRow, from view_semantic_evidence's
    own already-catalog-cross-checked referenced_tables_json (see
    data.view_semantics_service's own docstring: _extract_all_referenced_
    tables only ever returns a guess that resolves to a REAL table_fqn in
    this source's own catalog) — the "stored catalog dependency" source of
    truth this module treats as ground truth for object IDENTITY (never
    re-derived or second-guessed from the AST below)."""
    import json

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT table_fqn, definition_sql, referenced_tables_json "
            "FROM view_semantic_evidence WHERE source_id = ?",
            (source_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — fail open: no view evidence yet is a normal, valid state
        return {}
    finally:
        conn.close()

    out: dict[str, _ViewEvidenceRow] = {}
    for row in rows:
        d = dict(row)
        try:
            referenced = tuple(json.loads(d.get("referenced_tables_json") or "[]"))
        except (TypeError, ValueError):
            referenced = ()
        out[d["table_fqn"].lower()] = _ViewEvidenceRow(
            definition_sql=d.get("definition_sql"), referenced_tables=referenced,
        )
    return out


# ── AST derivation (sqlglot, dialect="tsql" — never regex-only) ────────────


@dataclass(frozen=True)
class _ViewAstFacts:
    """Structural facts derived from one view's OWN OUTER query only — never
    conflating a nested subquery's own filtering/aggregation with the
    view's own overall row grain (see _prune_nested_queries)."""

    has_where: bool
    has_having: bool
    has_group_by: bool
    has_distinct: bool
    has_set_operation: bool
    has_row_limit: bool
    has_root_level_aggregate: bool
    has_literal_comparison_filter: bool
    literal_projection_aliases: tuple[str, ...]
    anchor_table_fqn_lower: str | None
    join_kind_by_table_fqn_lower: Mapping[str, str]
    parse_error: str | None


def _prune_nested_queries(node: exp.Expression, root: exp.Expression) -> bool:
    """sqlglot Expression.walk()'s own `prune` callback: stop descending
    into a nested exp.Subquery/exp.Select — the boundary of "this view's own
    outer query" — except for `root` itself (which IS an exp.Select and
    must never be pruned, or nothing would ever be walked at all)."""
    return node is not root and isinstance(node, (exp.Subquery, exp.Select))


def _root_level_aggregate_functions(select: exp.Select) -> tuple[exp.AggFunc, ...]:
    return tuple(
        node for node in select.walk(prune=lambda n: _prune_nested_queries(n, select))
        if isinstance((node[0] if isinstance(node, tuple) else node), exp.AggFunc)
    )


def _root_level_literal_comparison(select: exp.Select) -> bool:
    """True when the OUTER query's own WHERE or HAVING clause contains a
    comparison (=, !=, <, <=, >, >=, IN) between a column and a hardcoded
    literal — the general, AST-based version of "a hidden business-slice
    filter", generalizing the Step 3D1 audit's own hand-confirmed findings
    (e.g. "WHERE AP.TrackID IN (1000)", "WHERE regularfee > 10 ...")."""
    comparison_types = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    for clause_key in ("where", "having"):
        clause = select.args.get(clause_key)
        if clause is None:
            continue
        for node in clause.walk(prune=lambda n: _prune_nested_queries(n, select)):
            n = node[0] if isinstance(node, tuple) else node
            if isinstance(n, comparison_types):
                if isinstance(n.left, exp.Column) and isinstance(n.right, exp.Literal):
                    return True
                if isinstance(n.right, exp.Column) and isinstance(n.left, exp.Literal):
                    return True
            elif isinstance(n, exp.In):
                if isinstance(n.this, exp.Column) and n.expressions and all(
                    isinstance(e, exp.Literal) for e in n.expressions
                ):
                    return True
    return False


def _literal_projection_aliases(select: exp.Select) -> tuple[str, ...]:
    """Column aliases whose projected VALUE is a hardcoded literal, not a
    real column reference (e.g. "'Some Fixed Label' AS Category") — the
    generalized version of the Step 3D1 read-only equivalence audit's own
    decisive finding on a real, similarly-shaped view (a business-looking
    column whose value never varies at all). Such a column can never
    satisfy a requirement for a real, canonical business attribute
    (grouping, filter, or identity), regardless of how plausible its alias
    name looks."""
    found: list[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Literal):
            found.append(projection.alias_or_name)
    return tuple(found)


def _table_fqn(table: exp.Table) -> str | None:
    """Phase 7, Step 6E3B5, Part 3 -- a table reference that omits its
    schema (`FROM SomeTable` rather than `FROM dbo.SomeTable`) resolves,
    in SQL Server, to the connection's own default schema -- `dbo` for
    every account this codebase's connectors are configured with (a
    non-default schema is always referenced explicitly-qualified in
    practice; an unqualified reference into a non-default schema would be
    a genuine, separate account-configuration question, not something
    this generic parser-level fix should guess around). Returning the
    bare table name with NO schema prefix here (the prior behavior) could
    never match `known_columns_by_fqn_lower`'s catalog-derived keys
    (always "schema.table"), silently stranding every column reference
    through that alias as unresolved regardless of how complete the
    underlying column metadata actually was -- a real generic defect, not
    a missing-metadata one."""
    if not isinstance(table, exp.Table) or not table.name:
        return None
    schema = table.db or "dbo"
    return f"{schema}.{table.name}"


def _derive_ast_facts(definition_sql: str | None) -> _ViewAstFacts:
    """Parse `definition_sql` (a stored CREATE VIEW statement, or a bare
    SELECT — both are accepted) with sqlglot(dialect="tsql") and derive
    every structural fact this module needs. Never raises: any parse
    failure (missing text, unparseable dialect quirk, more/less than one
    statement) is reported via `parse_error`, and the caller (see
    _classify_one) must treat that as UNKNOWN, never PROVEN_EQUIVALENT_VIEW
    or any other positive classification — "fail closed", per this step's
    own explicit requirement."""
    empty = _ViewAstFacts(
        has_where=False, has_having=False, has_group_by=False, has_distinct=False,
        has_set_operation=False, has_row_limit=False, has_root_level_aggregate=False,
        has_literal_comparison_filter=False, literal_projection_aliases=(),
        anchor_table_fqn_lower=None, join_kind_by_table_fqn_lower={},
        parse_error="no definition_sql available",
    )
    if not definition_sql or not isinstance(definition_sql, str) or not definition_sql.strip():
        return empty

    try:
        statements = [s for s in sqlglot.parse(definition_sql, read=TSQL_DIALECT) if s is not None]
    except Exception as exc:  # noqa: BLE001 — any parser failure fails closed, never raises
        return _ViewAstFacts(**{**empty.__dict__, "parse_error": f"{type(exc).__name__}: could not parse definition"})

    if len(statements) != 1:
        return _ViewAstFacts(**{**empty.__dict__, "parse_error": f"expected exactly one statement, found {len(statements)}"})

    tree = statements[0]
    if isinstance(tree, exp.Create):
        tree = tree.expression
    # A view body is sometimes wrapped in parentheses
    # ("CREATE VIEW x AS (SELECT ...)"), which sqlglot parses as an
    # exp.Subquery wrapping the real exp.Select/exp.SetOperation — unwrap
    # any number of such redundant parenthesized layers (never more than a
    # few in practice; unconditional loop bounded by "no more Subquery
    # wrapper found") before classifying the root statement type.
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if tree is None:
        return _ViewAstFacts(**{**empty.__dict__, "parse_error": "CREATE VIEW has no body expression"})

    if isinstance(tree, exp.SetOperation):
        # A UNION-shaped view: row-preservation/aggregation facts are
        # evaluated per branch and OR-combined (any branch narrowing the
        # result is enough to disqualify a universal-equivalence claim);
        # anchor/join facts are not meaningful for a set operation's own
        # top level, so they stay empty/None (never invented).
        branches = [b for b in (tree.left, tree.right) if isinstance(b, exp.Select)]
        has_where = any(bool(b.args.get("where")) for b in branches)
        has_having = any(bool(b.args.get("having")) for b in branches)
        has_group_by = any(bool(b.args.get("group")) for b in branches)
        has_distinct = any(bool(b.args.get("distinct")) for b in branches)
        has_row_limit = any(bool(b.args.get("limit")) for b in branches)
        has_root_level_aggregate = any(bool(_root_level_aggregate_functions(b)) for b in branches)
        has_literal_comparison_filter = any(_root_level_literal_comparison(b) for b in branches)
        literal_projection_aliases = tuple(
            alias for b in branches for alias in _literal_projection_aliases(b)
        )
        return _ViewAstFacts(
            has_where=has_where, has_having=has_having, has_group_by=has_group_by,
            has_distinct=has_distinct, has_set_operation=True, has_row_limit=has_row_limit,
            has_root_level_aggregate=has_root_level_aggregate,
            has_literal_comparison_filter=has_literal_comparison_filter,
            literal_projection_aliases=literal_projection_aliases,
            anchor_table_fqn_lower=None, join_kind_by_table_fqn_lower={}, parse_error=None,
        )

    if not isinstance(tree, exp.Select):
        return _ViewAstFacts(**{**empty.__dict__, "parse_error": f"unsupported root statement type {type(tree).__name__!r}"})

    from_clause = tree.args.get("from_")
    anchor_fqn = _table_fqn(from_clause.this) if from_clause is not None else None

    join_kinds: dict[str, str] = {}
    for join in tree.args.get("joins") or []:
        fqn = _table_fqn(join.this)
        if fqn is None:
            continue
        side = str(join.side or "").upper()
        kind = str(join.kind or "").upper()
        join_kinds[fqn.lower()] = side or kind or "INNER"

    return _ViewAstFacts(
        has_where=bool(tree.args.get("where")),
        has_having=bool(tree.args.get("having")),
        has_group_by=bool(tree.args.get("group")),
        has_distinct=bool(tree.args.get("distinct")),
        has_set_operation=False,
        has_row_limit=bool(tree.args.get("limit")),
        has_root_level_aggregate=bool(_root_level_aggregate_functions(tree)),
        has_literal_comparison_filter=_root_level_literal_comparison(tree),
        literal_projection_aliases=_literal_projection_aliases(tree),
        anchor_table_fqn_lower=anchor_fqn.lower() if anchor_fqn else None,
        join_kind_by_table_fqn_lower=join_kinds,
        parse_error=None,
    )


# ── Dependency closure (stored catalog dependencies are the ONLY source of
#    object identity here — AST facts above never invent one) ─────────────


def _view_dependency_closure(
    start_fqn_lower: str,
    view_evidence_by_fqn_lower: Mapping[str, _ViewEvidenceRow],
    object_type_by_fqn_lower: Mapping[str, str],
) -> tuple[str, ...]:
    """Every object this view depends on, directly or transitively THROUGH
    OTHER VIEWS ONLY (a table has no further catalog dependencies of its
    own to chase), up to _MAX_VIEW_CLOSURE_DEPTH hops — bounded, cycle-safe
    (a `seen` set), deterministic (sorted output). Uses ONLY view_semantic_
    evidence's own already-catalog-cross-checked referenced_tables — never
    a name guess, never an AST-derived table name that isn't already in
    that stored list."""
    seen: set[str] = {start_fqn_lower}
    frontier = [start_fqn_lower]
    depth = 0
    while frontier and depth < _MAX_VIEW_CLOSURE_DEPTH:
        next_frontier: list[str] = []
        for fqn_lower in frontier:
            evidence = view_evidence_by_fqn_lower.get(fqn_lower)
            if evidence is None:
                continue
            for dep in evidence.referenced_tables:
                dep_lower = dep.lower()
                if dep_lower in seen:
                    continue
                seen.add(dep_lower)
                next_frontier.append(dep_lower)
        frontier = next_frontier
        depth += 1
    seen.discard(start_fqn_lower)
    return tuple(sorted(seen))


# ── Per-object classification (the precedence ladder) ──────────────────────


def _classify_one(
    object_fqn: str,
    object_fqn_lower: str,
    object_type: str,
    retrieval_tier: int,
    *,
    canonical_by_fqn_lower: Mapping[str, tuple[str, ...]],
    preferred_view_by_fqn_lower: Mapping[str, tuple[str, ...]],
    canonical_fqns_lower: frozenset[str],
    view_evidence_by_fqn_lower: Mapping[str, _ViewEvidenceRow],
    object_type_by_fqn_lower: Mapping[str, str],
    tier_by_fqn_lower: Mapping[str, int],
    column_names_by_fqn_lower: Mapping[str, frozenset[str]],
    primary_key_columns_by_fqn_lower: Mapping[str, tuple[str, ...]],
) -> SchemaObjectAuthority:
    own_canonical_entities = canonical_by_fqn_lower.get(object_fqn_lower, ())
    own_preferred_entities = preferred_view_by_fqn_lower.get(object_fqn_lower, ())

    # ── Declared authority always wins outright, independent of any
    #    structural proof below (Phase 7 Step 3E, Part 2's own rule) ──────
    if own_preferred_entities and object_type == "VIEW":
        return SchemaObjectAuthority(
            object_fqn=object_fqn, object_type=object_type,
            canonical_table_fqns=tuple(sorted(own_canonical_entities))[:_MAX_CANONICAL_TABLE_FQNS],
            preferred_view_for_entities=own_preferred_entities[:_MAX_PREFERRED_VIEW_ENTITIES],
            authority_class=AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
            restriction_status=RESTRICTION_STATUS_UNKNOWN, restrictions=(),
            grain_class=GRAIN_CLASS_UNKNOWN, aggregation_status="NONE",
            row_preservation_status=ROW_PRESERVATION_UNKNOWN,
            dependency_fqns=(), unsafe_dependency_fqns=(),
            evidence=(f"declared_preferred_view:{','.join(own_preferred_entities)}",),
            warnings=(),
        )

    if object_type != "VIEW":
        # A plain table/other object: its own tier is the only authority
        # fact that applies. A registered canonical_table_fqn is trusted
        # evidence; the absence of one is NEVER treated as "not canonical"
        # (Part 2: "do not infer canonical status from a generic-looking
        # name alone") — it is simply UNKNOWN (no positive claim either
        # way), unless the object's own tier already marks it unsafe.
        if retrieval_tier in UNSAFE_RETRIEVAL_TIERS:
            authority_class = AUTHORITY_CLASS_UNSAFE_DEPENDENCY
            evidence = (f"unsafe_retrieval_tier:{retrieval_tier}",)
        elif own_canonical_entities:
            authority_class = AUTHORITY_CLASS_CANONICAL_TABLE
            evidence = (f"canonical_table_for:{','.join(own_canonical_entities)}",)
        else:
            authority_class = AUTHORITY_CLASS_UNKNOWN
            evidence = ()
        return SchemaObjectAuthority(
            object_fqn=object_fqn, object_type=object_type,
            canonical_table_fqns=tuple(sorted(own_canonical_entities))[:_MAX_CANONICAL_TABLE_FQNS],
            preferred_view_for_entities=(),
            authority_class=authority_class,
            restriction_status=RESTRICTION_STATUS_NONE, restrictions=(),
            grain_class=GRAIN_CLASS_BASE_ROW, aggregation_status="NONE",
            row_preservation_status=ROW_PRESERVATION_PRESERVED,
            dependency_fqns=(), unsafe_dependency_fqns=(),
            evidence=evidence[:_MAX_EVIDENCE_CODES], warnings=(),
        )

    # ── VIEW: structural derivation ──────────────────────────────────────
    evidence_row = view_evidence_by_fqn_lower.get(object_fqn_lower)
    ast_facts = _derive_ast_facts(evidence_row.definition_sql if evidence_row else None)

    if ast_facts.parse_error is not None or evidence_row is None:
        warning = ast_facts.parse_error or "no stored view definition/dependency evidence"
        return SchemaObjectAuthority(
            object_fqn=object_fqn, object_type=object_type,
            canonical_table_fqns=(), preferred_view_for_entities=(),
            authority_class=AUTHORITY_CLASS_UNKNOWN,
            restriction_status=RESTRICTION_STATUS_UNKNOWN, restrictions=(),
            grain_class=GRAIN_CLASS_UNKNOWN, aggregation_status="UNKNOWN",
            row_preservation_status=ROW_PRESERVATION_UNKNOWN,
            dependency_fqns=(), unsafe_dependency_fqns=(),
            evidence=(), warnings=(warning,)[:_MAX_WARNINGS],
        )

    dependency_fqns_lower = frozenset(d.lower() for d in evidence_row.referenced_tables)
    closure_lower = frozenset(_view_dependency_closure(object_fqn_lower, view_evidence_by_fqn_lower, object_type_by_fqn_lower))
    full_closure_lower = dependency_fqns_lower | closure_lower

    unsafe_deps = tuple(sorted(
        fqn for fqn in full_closure_lower
        if tier_by_fqn_lower.get(fqn) in UNSAFE_RETRIEVAL_TIERS
    ))[:_MAX_UNSAFE_DEPENDENCY_FQNS]

    reached_canonical_lower = sorted(full_closure_lower & canonical_fqns_lower)
    reached_canonical_entities = tuple(sorted({
        entity for fqn in reached_canonical_lower for entity in canonical_by_fqn_lower.get(fqn, ())
    }))[:_MAX_CANONICAL_TABLE_FQNS]

    evidence: list[str] = []
    warnings: list[str] = []

    restriction_kinds: list[str] = []
    if ast_facts.has_literal_comparison_filter:
        restriction_kinds.append("where_or_having_literal_filter")
    if ast_facts.literal_projection_aliases:
        restriction_kinds.append("hardcoded_literal_projected_column")
        evidence.append(f"literal_projected_columns:{','.join(ast_facts.literal_projection_aliases[:5])}")
    if ast_facts.has_row_limit:
        restriction_kinds.append("row_limit")
    restriction_status = RESTRICTION_STATUS_FILTERED if restriction_kinds else RESTRICTION_STATUS_NONE

    if ast_facts.has_group_by or ast_facts.has_root_level_aggregate:
        grain_class = GRAIN_CLASS_AGGREGATED
    elif ast_facts.has_distinct:
        grain_class = GRAIN_CLASS_DEDUPLICATED
    else:
        grain_class = GRAIN_CLASS_BASE_ROW

    aggregation_parts = []
    if ast_facts.has_group_by:
        aggregation_parts.append("GROUP_BY")
    if ast_facts.has_root_level_aggregate:
        aggregation_parts.append("AGGREGATE_FUNCTION")
    if ast_facts.has_distinct:
        aggregation_parts.append("DISTINCT")
    if ast_facts.has_set_operation:
        aggregation_parts.append("SET_OPERATION")
    aggregation_status = "+".join(aggregation_parts) if aggregation_parts else "NONE"

    # Row preservation: an at-risk INNER JOIN is any join to a dependency
    # OTHER than the anchor table itself whose kind is not LEFT/FULL — this
    # module never upgrades that to PRESERVED without positive completeness
    # evidence (fail closed; see this module's own docstring), so any such
    # join is AT_RISK even when nothing else is wrong.
    inner_join_targets = tuple(sorted(
        fqn for fqn, kind in ast_facts.join_kind_by_table_fqn_lower.items()
        if kind not in ("LEFT", "FULL") and fqn != ast_facts.anchor_table_fqn_lower
    ))
    if restriction_kinds:
        row_preservation_status = ROW_PRESERVATION_REDUCED
    elif inner_join_targets:
        row_preservation_status = ROW_PRESERVATION_AT_RISK
        evidence.append(f"inner_join_unverified:{','.join(inner_join_targets[:5])}")
    else:
        row_preservation_status = ROW_PRESERVATION_PRESERVED

    if unsafe_deps:
        evidence.append(f"unsafe_dependency:{','.join(unsafe_deps[:5])}")
        authority_class = AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    elif grain_class == GRAIN_CLASS_AGGREGATED:
        evidence.append(f"aggregation:{aggregation_status}")
        authority_class = AUTHORITY_CLASS_INCOMPATIBLE_GRAIN
    elif restriction_kinds:
        evidence.extend(f"restriction:{k}" for k in restriction_kinds)
        authority_class = AUTHORITY_CLASS_RESTRICTED_VIEW
    elif ast_facts.has_distinct or ast_facts.has_set_operation or inner_join_targets:
        if ast_facts.has_distinct:
            evidence.append("distinct_deduplication")
        if ast_facts.has_set_operation:
            evidence.append("set_operation")
        authority_class = AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW
    elif reached_canonical_entities:
        # Full proof: pure LEFT-JOIN-or-anchor-only chain, no restriction,
        # no aggregation/distinct/set-op, and it reaches a real canonical
        # table. Require the canonical table's own primary-key column to
        # still be safely available (by name, or via an anchor wildcard
        # projection) — "required canonical columns... remain available".
        pk_available = False
        own_columns = column_names_by_fqn_lower.get(object_fqn_lower, frozenset())
        for canon_fqn_lower in reached_canonical_lower:
            pk_cols = primary_key_columns_by_fqn_lower.get(canon_fqn_lower, ())
            if any(pk.lower() in own_columns for pk in pk_cols):
                pk_available = True
                break
            if canon_fqn_lower == ast_facts.anchor_table_fqn_lower:
                pk_available = True  # the anchor's own row identity is intact by construction
                break
        if pk_available:
            evidence.append(f"dependency_reaches_canonical:{','.join(reached_canonical_lower[:5])}")
            authority_class = AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW
        else:
            warnings.append("canonical primary-key column not found among this view's own projected columns")
            authority_class = AUTHORITY_CLASS_CONDITIONALLY_COMPATIBLE_VIEW
    else:
        authority_class = AUTHORITY_CLASS_UNKNOWN

    return SchemaObjectAuthority(
        object_fqn=object_fqn, object_type=object_type,
        canonical_table_fqns=reached_canonical_entities,
        preferred_view_for_entities=(),
        authority_class=authority_class,
        restriction_status=restriction_status,
        restrictions=tuple(restriction_kinds),
        grain_class=grain_class,
        aggregation_status=aggregation_status,
        row_preservation_status=row_preservation_status,
        dependency_fqns=tuple(sorted(dependency_fqns_lower))[:_MAX_DEPENDENCY_FQNS],
        unsafe_dependency_fqns=unsafe_deps,
        evidence=tuple(evidence)[:_MAX_EVIDENCE_CODES],
        warnings=tuple(warnings)[:_MAX_WARNINGS],
    )


def classify_framework_backed_eligibility(
    authority: SchemaObjectAuthority,
    *,
    lineage_complete: bool,
    has_business_output_columns: bool,
    selects_secret_column: bool,
) -> str:
    """Phase 7, Step 6E3B, Part 5 downstream refinement (see the two new
    class constants' own docstring above for why this is never wired into
    _classify_one itself). Widens eligibility for a view this module's own
    structural pass classified AUTHORITY_CLASS_UNSAFE_DEPENDENCY ONLY --
    never touches, narrows, or reinterprets any other authority_class.

    Returns AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW when the view:
    exposes at least one real (non-literal, non-secret) business output
    column, has fully resolved output-column lineage, and selects no
    authentication/credential secret column. Returns AUTHORITY_CLASS_
    LINEAGE_INCOMPLETE when it would otherwise qualify but lineage is not
    resolved enough to trust -- fail closed, never silently upgraded to
    FRAMEWORK_BACKED_BUSINESS_VIEW without that proof. Returns the
    original authority_class unchanged in every other case. Per this
    milestone's own explicit requirement, this NEVER promotes a view to
    CANONICAL_TABLE/DECLARED_PREFERRED_VIEW/PROVEN_EQUIVALENT_VIEW --
    FRAMEWORK_BACKED_BUSINESS_VIEW remains its own, narrower-trust class."""
    if authority.authority_class != AUTHORITY_CLASS_UNSAFE_DEPENDENCY:
        return authority.authority_class
    if selects_secret_column or not has_business_output_columns:
        return authority.authority_class
    if not lineage_complete:
        return AUTHORITY_CLASS_LINEAGE_INCOMPLETE
    return AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW


# ── Public batch entry point ────────────────────────────────────────────────


def compute_schema_object_authority(
    documents: Sequence, identity: SchemaObjectAuthorityIdentity,
) -> SchemaObjectAuthorityResult:
    """Build a SchemaObjectAuthorityResult from an already-loaded document
    sequence (any object exposing object_fqn/object_type/retrieval_tier/
    metadata — typically core.sqlservice.vector_index.VectorIndex.documents,
    the same sequence core.sqlservice.canonical_scoring/business_concept_
    index already consume) plus `identity.source_id` (used ONLY to read
    semantic_entity_contracts/view_semantic_evidence — never a live query,
    never a question). Every object in `documents` gets a result — none are
    excluded, mirroring every other per-build pass in this package."""
    fqn_by_lower: dict[str, str] = {}
    object_type_by_fqn_lower: dict[str, str] = {}
    tier_by_fqn_lower: dict[str, int] = {}
    column_names_by_fqn_lower: dict[str, frozenset[str]] = {}
    primary_key_columns_by_fqn_lower: dict[str, tuple[str, ...]] = {}

    for doc in documents:
        key = doc.object_fqn.lower()
        fqn_by_lower[key] = doc.object_fqn
        object_type_by_fqn_lower[key] = doc.object_type
        tier_by_fqn_lower[key] = doc.retrieval_tier
        column_names_by_fqn_lower[key] = frozenset(
            str(c).lower() for c in (doc.metadata.get("column_names") or ())
        )
        primary_key_columns_by_fqn_lower[key] = tuple(doc.metadata.get("primary_key_columns") or ())

    contracts = _load_semantic_contracts(identity.source_id)
    canonical_by_fqn_lower: dict[str, list[str]] = {}
    preferred_view_by_fqn_lower: dict[str, list[str]] = {}
    for contract in contracts:
        entity_name = contract.get("entity_name") or ""
        canonical_fqn = contract.get("canonical_table_fqn")
        preferred_fqn = contract.get("preferred_view_fqn")
        if canonical_fqn and canonical_fqn.lower() in fqn_by_lower:
            canonical_by_fqn_lower.setdefault(canonical_fqn.lower(), []).append(entity_name)
        if preferred_fqn and preferred_fqn.lower() in fqn_by_lower:
            preferred_view_by_fqn_lower.setdefault(preferred_fqn.lower(), []).append(entity_name)
    canonical_fqns_lower = frozenset(canonical_by_fqn_lower.keys())
    canonical_by_fqn_lower_t = {k: tuple(sorted(v)) for k, v in canonical_by_fqn_lower.items()}
    preferred_view_by_fqn_lower_t = {k: tuple(sorted(v)) for k, v in preferred_view_by_fqn_lower.items()}

    view_evidence_by_fqn_lower = _load_view_semantic_evidence(identity.source_id)

    authorities: dict[str, SchemaObjectAuthority] = {}
    for fqn_lower, object_fqn in fqn_by_lower.items():
        authorities[fqn_lower] = _classify_one(
            object_fqn, fqn_lower, object_type_by_fqn_lower[fqn_lower], tier_by_fqn_lower[fqn_lower],
            canonical_by_fqn_lower=canonical_by_fqn_lower_t,
            preferred_view_by_fqn_lower=preferred_view_by_fqn_lower_t,
            canonical_fqns_lower=canonical_fqns_lower,
            view_evidence_by_fqn_lower=view_evidence_by_fqn_lower,
            object_type_by_fqn_lower=object_type_by_fqn_lower,
            tier_by_fqn_lower=tier_by_fqn_lower,
            column_names_by_fqn_lower=column_names_by_fqn_lower,
            primary_key_columns_by_fqn_lower=primary_key_columns_by_fqn_lower,
        )

    return SchemaObjectAuthorityResult(
        identity=identity, authorities_by_fqn_lower=authorities, document_count=len(fqn_by_lower),
    )


# ── Bounded in-process cache (mirrors core.sqlservice.canonical_scoring's) ──

_MAX_CACHE_ENTRIES = 16
_authority_cache: "dict[SchemaObjectAuthorityIdentity, SchemaObjectAuthorityResult]" = {}
_authority_cache_order: list = []


def load_schema_object_authority(
    documents: Sequence, build_id: int, source_id: int, schema_fingerprint: str | None = None,
    *, use_cache: bool = True,
) -> SchemaObjectAuthorityResult:
    """Build (or reuse a cached) SchemaObjectAuthorityResult. Cached under
    (build_id, source_id, schema_fingerprint, authority_state,
    contract_version) — invalidates automatically whenever
    semantic_entity_contracts/view_semantic_evidence/sql_view_semantic_
    enrichments change for this source (see _authority_state), whenever the
    build changes (a new schema always gets a new build_id), or whenever
    this module's own classification logic changes (AUTHORITY_CONTRACT_
    VERSION). Never stores question data, embeddings, prompts, or
    credentials — the cached value is exactly the bounded, immutable
    SchemaObjectAuthority set this module's own docstring describes."""
    identity = SchemaObjectAuthorityIdentity(
        build_id=build_id, source_id=source_id, schema_fingerprint=schema_fingerprint,
        authority_state=_authority_state(source_id),
    )
    if use_cache and identity in _authority_cache:
        _authority_cache_order.remove(identity)
        _authority_cache_order.append(identity)
        return _authority_cache[identity]

    result = compute_schema_object_authority(documents, identity)
    if use_cache:
        _authority_cache[identity] = result
        _authority_cache_order.append(identity)
        while len(_authority_cache_order) > _MAX_CACHE_ENTRIES:
            oldest = _authority_cache_order.pop(0)
            _authority_cache.pop(oldest, None)
    return result


def clear_schema_object_authority_cache() -> None:
    """Explicitly evict every cached SchemaObjectAuthorityResult — for
    tests, mirroring clear_canonical_scoring_cache()/
    clear_business_concept_index_cache()."""
    _authority_cache.clear()
    _authority_cache_order.clear()


def schema_object_authority_cache_size() -> int:
    return len(_authority_cache)
