"""
Deterministic, persisted VIEW profiling -- Phase 7, Step 6E3B.

Computes one ViewProfile per view from metadata this codebase ALREADY
persists -- never a new live probe, never an OpenAI/embedding call, never
raw row values. Reuses:

  - view_semantic_evidence's own stored `definition_sql` and
    catalog-cross-checked `referenced_tables_json` (data.view_semantics_
    service) -- the same "stored catalog dependency is the only source of
    object identity" contract core.sqlservice.schema_object_authority
    already established; this module never second-guesses that list.
  - core.sqlservice.schema_object_authority's own sqlglot(dialect="tsql")
    AST primitives (_derive_ast_facts, _view_dependency_closure,
    _table_fqn, _prune_nested_queries) -- imported and reused directly
    rather than re-implemented, per this milestone's own instruction to
    extend existing architecture instead of building a disconnected
    parallel system.
  - core.dictionary.pii_detector's existing PII/secret-category signal
    (detect_pii_tier / is_restricted_category) for the PII/secret
    metadata carried on the profile -- informational only here; the
    unconditional BLOCK enforcement lives in core.sqlservice.sql_
    validation, not this module.

This module is pure computation: no database access, no network access,
never raises (a parse/resolution failure is captured as a typed,
persisted reason -- see FAILURE_*/LINEAGE_UNRESOLVED_* constants -- never
silently upgraded to a positive claim). Persistence (data/view_profile_
service.py) and batch orchestration are a separate, DB-touching layer
that calls compute_view_profile() per view.

Phase 7, Step 6E3B3 -- coverage-expansion additions on top of 6E3B/6E3B1/
6E3B2, all additive extensions of the SAME deterministic pass (never a
parallel parser, per this step's own explicit instruction):

  - Derived-table and CTE output columns are now resolved as named
    "scopes" (alias -> {column_name: OutputColumnLineage}), uniformly,
    so an outer query's reference to `d.Col` (a FROM/JOIN derived table
    alias) resolves exactly like a CTE reference already did -- see
    _build_scopes/_resolve_subquery_scope. This is the single highest-
    impact fix identified by this step's own failure inventory: most
    "unresolvable_table_alias"/"column_not_found" cases were views whose
    FROM/JOIN target was a derived table, which the prior alias map
    silently dropped (only real exp.Table sources were ever registered).
  - `SELECT *` now expands safely across EVERY known FROM/JOIN/scope
    target (previously only when exactly one target existed) -- each
    projected column is positionally attributed to its own real source
    in FROM/JOIN declaration order, which is exactly what SQL Server
    itself does; this is not a guess (a same-named column from two
    different sources is still two distinct, individually-resolved
    output columns, matching this module's own "never infer lineage from
    matching names alone" rule -- no column's SOURCE is ever decided by
    name-matching here, only its POSITION in the star expansion is).
    Qualified star (`t.*`) is scoped to just that one alias.
  - CROSS/OUTER APPLY is resolved as another scope (sqlglot's own
    exp.Lateral wrapping a correlated exp.Subquery/exp.Select) when the
    AST exposes it structurally, per this step's own "where sqlglot
    exposes sufficient AST evidence" allowance.
  - A UNION chain of 3+ branches (sqlglot nests these as
    SetOperation(SetOperation(A, B), C)) is now fully flattened --
    previously only the two immediate tree.left/tree.right children were
    inspected, silently dropping every branch beyond the first two for a
    3+-way UNION (a latent correctness risk, not just a coverage gap).
  - A definition_sql landing at EXACTLY _SUSPECTED_TRUNCATION_LENGTH
    characters is flagged `definition_truncated_suspected` -- this
    module's own failure-inventory audit found 41 of 42 PARSE_ERROR
    views (and 48 of 240 total) truncated at precisely this length,
    traced to data.view_semantics_service's reuse of a generic value-
    SAMPLING read path (intended for a handful of representative row
    values, not a full object definition) to capture `definition_sql`.
    Fixing that capture requires a live SQL Server read and is explicit
    out-of-scope for this module (pure computation, no live connection)
    -- this flag only ensures a truncated definition is never silently
    trusted (it disqualifies LIMITED_COMPLETE outright, see below), and
    is labeled honestly rather than folded into a generic PARSE_ERROR.
  - PROFILE_STATE_LIMITED_COMPLETE / PROFILE_STATE_UNSAFE (Part 3/4): a
    layered-fallback runtime state for a view whose full expression
    lineage cannot be resolved end-to-end, but whose catalog-confirmed
    output-column set, dependency closure, and restriction/grain facts
    are ALL known with no unproven secret-exposure risk -- see
    ViewProfile.limited_complete_eligible and classify_profile_state's
    own updated docstring for the exact, fail-closed eligibility bar.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import sqlglot
from sqlglot import exp

from core.dictionary.pii_detector import SECRET_POLICY_VERSION, detect_pii_tier, is_restricted_category
from core.sqlservice import schema_object_authority as _authority

TSQL_DIALECT = "tsql"

# Bump whenever this module's DERIVATION LOGIC changes in a way that could
# change a prior result for unchanged inputs -- forces every persisted
# sql_view_profiles row for the old version to be recomputed (see
# data.view_profile_service's own version-comparison reuse logic). Never
# bump for a pure refactor with identical behavior.
VIEW_PROFILE_CONTRACT_VERSION = 1

_MAX_CTE_RESOLUTION_DEPTH = 2
_MAX_OUTPUT_COLUMNS = 200
_MAX_LIST_FIELD = 25

# Phase 7, Step 6E3B3 -- bounds the recursion for resolving a derived-table/
# CTE's OWN output lineage (which may itself contain further derived
# tables/CTEs) before it can be exposed as a named scope to an outer query.
# A real view catalog nests a handful of levels at most; this bound exists
# only to guarantee termination on a pathological/adversarial input, never
# expected to bind on a real view in practice.
_MAX_SCOPE_RESOLUTION_DEPTH = 6

# data.view_semantics_service's own definition_sql capture (Phase 7, Step
# 6E3B3's own failure-inventory audit) reuses a generic value-SAMPLING read
# path whose string truncation lands at exactly this many characters --
# see this module's own docstring. A stored definition_sql at precisely
# this length is therefore SUSPECTED truncated (cut off mid-statement),
# never confirmed complete, regardless of whether it happens to parse.
_SUSPECTED_TRUNCATION_LENGTH = 4000

# Phase 7, Step 6E3B3, Part 4 -- the minimum fraction of a view's OWN
# output columns that must be fully resolved (non-UNRESOLVED) before an
# otherwise-INCOMPLETE profile may even be CONSIDERED for LIMITED_COMPLETE
# eligibility. A view where most of its own projection is still a black
# box is never "limited but usable" -- it is simply incomplete.
_LIMITED_COMPLETE_MIN_RESOLVED_RATIO = 0.5

# ── Completion / failure vocabulary ─────────────────────────────────────

COMPLETION_COMPLETE = "COMPLETE"
COMPLETION_INCOMPLETE = "INCOMPLETE"
COMPLETION_FAILED = "FAILED"

# ── Runtime profile-trust states (Phase 7, Step 6E3B1, Part 2) ────────────
#
# The ONE place every consumer (core.sqlservice.schema_frontier's admission
# refinement, core.sqlservice.sql_validation's suitability check, any
# future caller) decides whether a persisted ViewProfile may be trusted for
# a given build. Fails closed by construction: anything other than
# PROFILE_STATE_COMPLETE must never be treated as "safe to use for an
# unrestricted count, global population claim, or secret-sensitive query"
# by a caller -- see classify_profile_state's own docstring.
PROFILE_STATE_COMPLETE = "COMPLETE"
PROFILE_STATE_LINEAGE_INCOMPLETE = "LINEAGE_INCOMPLETE"
PROFILE_STATE_TYPED_FAILURE = "TYPED_FAILURE"
PROFILE_STATE_MISSING = "MISSING"
PROFILE_STATE_STALE = "STALE"

# Phase 7, Step 6E3B3, Part 3/4 -- a layered-fallback state, strictly
# NARROWER-trust than PROFILE_STATE_COMPLETE, never a substitute for it.
# See ViewProfile.limited_complete_eligible's own computation (Part 4's
# exact eligibility bar) and classify_profile_state's updated docstring.
PROFILE_STATE_LIMITED_COMPLETE = "LIMITED_COMPLETE"

# A profile that WOULD otherwise be LIMITED_COMPLETE-eligible except for an
# unresolved or proven secret-exposure risk -- distinguishes "blocked for
# safety" from "blocked for generic incompleteness" in the state
# vocabulary (Part 3's own explicit ask), never downgrades an already-
# COMPLETE profile (that enforcement remains core.sqlservice.sql_
# validation's, per this module's own docstring -- unchanged by this
# state).
PROFILE_STATE_UNSAFE = "UNSAFE"

FAILURE_NO_DEFINITION = "NO_DEFINITION_SQL"
FAILURE_PARSE_ERROR = "PARSE_ERROR"
FAILURE_UNSUPPORTED_STATEMENT = "UNSUPPORTED_STATEMENT_TYPE"

# ── Lineage vocabulary ───────────────────────────────────────────────────

LINEAGE_CONFIDENCE_HIGH = "HIGH"
LINEAGE_CONFIDENCE_MEDIUM = "MEDIUM"
LINEAGE_CONFIDENCE_LOW = "LOW"
LINEAGE_CONFIDENCE_UNRESOLVED = "UNRESOLVED"

TRANSFORM_PASSTHROUGH = "PASSTHROUGH"
TRANSFORM_EXPRESSION = "EXPRESSION"
TRANSFORM_AGGREGATE = "AGGREGATE"
TRANSFORM_CASE = "CASE"
TRANSFORM_LITERAL = "LITERAL"
TRANSFORM_WINDOW = "WINDOW"
TRANSFORM_STAR_EXPANSION = "STAR_EXPANSION"
TRANSFORM_UNKNOWN = "UNKNOWN"

LINEAGE_UNRESOLVED_AMBIGUOUS_STAR = "star_expansion_ambiguous_multi_table_source"
LINEAGE_UNRESOLVED_NESTED_SUBQUERY = "nested_subquery_column_source"
LINEAGE_UNRESOLVED_DEEP_CTE = "cte_reference_beyond_resolution_depth"
LINEAGE_UNRESOLVED_UNKNOWN_ALIAS = "unresolvable_table_alias"
LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED = "unqualified_column_multiple_candidate_sources"
LINEAGE_UNRESOLVED_NO_MATCHING_COLUMN = "column_not_found_on_any_known_source"
LINEAGE_UNRESOLVED_UNSUPPORTED_EXPR = "unsupported_expression_shape"
LINEAGE_UNRESOLVED_UNION_BRANCH_MISMATCH = "union_branch_lineage_unresolved"

# ── Duplication / suitability vocabulary ────────────────────────────────

DUPLICATION_RISK_NONE = "NONE"
DUPLICATION_RISK_POSSIBLE = "POSSIBLE_FANOUT"
DUPLICATION_RISK_LIKELY = "LIKELY_FANOUT"
DUPLICATION_RISK_UNKNOWN = "UNKNOWN"

SNAPSHOT_STATUS_CURRENT = "CURRENT"
SNAPSHOT_STATUS_SNAPSHOT_OR_STALE = "SNAPSHOT_OR_STALE_COPY"

CROSS_SOURCE_STATUS_SINGLE_SOURCE = "SINGLE_SOURCE"

# ── Semantic-enrichment consumption status (Phase 7, Step 6E3B1, Part 4) ──
#
# core.sqlservice.semantic_enrichment_store's sql_view_semantic_enrichments
# already has a real, active reader elsewhere (core.sqlservice.
# business_concept_index/schema_plan_options) -- this module READS that
# same already-populated table (never generates a row, never calls an AI
# provider) to fold business_name/description/source_role into a
# ViewProfile as optional, lowest-priority descriptive evidence. A typed
# status is always recorded, never silently omitted:
ENRICHMENT_STATUS_AVAILABLE = "AVAILABLE"  # exact identity proven current -- see data.view_profile_service's own docstring for why this is rarely reachable today
ENRICHMENT_STATUS_UNVERIFIED = "UNVERIFIED"  # a row exists, is not PROVABLY stale, but exact content-hash identity could not be verified (Step 6E3B2, Part 2)
ENRICHMENT_STATUS_MISSING = "MISSING"      # no ACTIVE row exists for this object at all
ENRICHMENT_STATUS_STALE = "STALE"          # provably outdated: predates this view's own last-discovered definition, or a superseded enrichment contract version
ENRICHMENT_STATUS_DISABLED = "DISABLED"    # enrichment GENERATION is off (never consumption -- see data.view_profile_service)
ENRICHMENT_STATUS_FAILED = "FAILED"        # a row exists but its payload could not be decoded

PURPOSE_DETAIL = "DETAIL"
PURPOSE_COUNT = "COUNT"
PURPOSE_AGGREGATION = "AGGREGATION"
PURPOSE_JOIN = "JOIN"


# ── Public data model ────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutputColumnLineage:
    output_name: str
    transformation_type: str
    source_refs: tuple[tuple[str, str], ...]  # (object_fqn_lower_or_alias, column_name)
    confidence: str
    unresolved_reason: str | None = None
    # Phase 7, Step 6E3B5, Part 2 -- for an UNRESOLVED column only: the
    # real, specific set of tables this column could plausibly be sourced
    # from, when that set is actually knowable (e.g. "one of these N
    # FROM/JOIN tables" for an ambiguous unqualified reference) -- NOT the
    # view's entire transitive dependency closure. `None` means "genuinely
    # opaque, no bounded candidate set could be derived" (a scalar
    # subquery, an unregistered alias, a CTE whose own resolution already
    # gave up) -- callers must treat `None` conservatively. `()` means
    # "proven zero column reference" (a pure literal/function-only
    # expression, or a star expansion with no targets at all) -- callers
    # may treat `()` as proof this column can never be a secret-column
    # passthrough, regardless of what the view's dependencies contain
    # elsewhere. This field is never consulted for a RESOLVED column
    # (source_refs already proves the exact source there).
    candidate_source_fqns: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ViewSuitability:
    """Persisted as `ViewSuitabilityProfile` per Part 5 -- suitability
    always depends on the REQUESTED operation, never a single blanket
    usability verdict (INCOMPATIBLE_GRAIN no longer means "unusable for
    every question", per this milestone's own explicit requirement)."""

    suitable_for_detail: bool
    suitable_for_count: bool
    suitable_for_aggregation: bool
    suitable_for_join: bool
    unsuitable_reasons: tuple[str, ...]
    required_distinct_key: str | None
    population_restriction: str | None
    confidence: str


@dataclass(frozen=True)
class ViewProfile:
    object_fqn: str
    source_id: int
    build_id: int | None
    schema_fingerprint: str | None
    definition_hash: str | None
    profiling_version: int
    completion_status: str
    failure_reason: str | None

    output_columns: tuple[OutputColumnLineage, ...] = ()
    direct_dependencies: tuple[str, ...] = ()
    transitive_dependencies: tuple[str, ...] = ()
    join_types: tuple[str, ...] = ()
    where_restrictions: tuple[str, ...] = ()
    having_restrictions: tuple[str, ...] = ()
    literal_projections: tuple[str, ...] = ()
    has_distinct: bool = False
    group_by_columns: tuple[str, ...] = ()
    aggregate_functions: tuple[str, ...] = ()
    has_union: bool = False
    union_branch_count: int = 0
    window_functions: tuple[str, ...] = ()
    has_top_limit: bool = False

    grain_class: str = _authority.GRAIN_CLASS_UNKNOWN
    grain_confidence: str = LINEAGE_CONFIDENCE_UNRESOLVED
    entity_keys: tuple[str, ...] = ()
    required_distinct_key: str | None = None
    duplication_risk: str = DUPLICATION_RISK_UNKNOWN
    row_preservation_risk: str = _authority.ROW_PRESERVATION_UNKNOWN
    population_restrictions: tuple[str, ...] = ()

    suitability: ViewSuitability | None = None

    authority_class: str = _authority.AUTHORITY_CLASS_UNKNOWN
    is_framework_backed: bool = False
    lineage_complete: bool = False

    pii_categories: tuple[str, ...] = ()
    secret_columns: tuple[str, ...] = ()
    snapshot_status: str = SNAPSHOT_STATUS_CURRENT
    cross_source_status: str = CROSS_SOURCE_STATUS_SINGLE_SOURCE

    semantic_enrichment_status: str = ENRICHMENT_STATUS_MISSING
    semantic_enrichment_business_name: str | None = None
    semantic_enrichment_description: str | None = None
    semantic_enrichment_source_role: str | None = None

    # Phase 7, Step 6E3B2, Part 1 -- the secret/PII policy version active
    # AT COMPUTE TIME (core.dictionary.pii_detector.SECRET_POLICY_VERSION).
    # classify_profile_state compares this against the CURRENT version at
    # validation time so a profile computed under an older, narrower/wider
    # secret definition is never trusted as COMPLETE after that policy
    # changes -- it must be re-profiled first.
    secret_policy_version: int = 0

    # Phase 7, Step 6E3B3, Part 2/3/4 -- see this module's own docstring
    # addendum. definition_truncated_suspected is independent of
    # completion_status (a truncated definition can still happen to parse
    # as a syntactically-valid prefix) and unconditionally disqualifies
    # LIMITED_COMPLETE eligibility below.
    definition_truncated_suspected: bool = False
    limited_complete_eligible: bool = False
    limited_complete_reasons: tuple[str, ...] = ()

    evidence: tuple[str, ...] = ()
    created_at: str = ""
    updated_at: str = ""


def _failed(
    object_fqn: str, source_id: int, build_id: int | None, schema_fingerprint: str | None,
    definition_hash: str | None, status: str, reason: str, now: str,
    *, definition_truncated_suspected: bool = False,
) -> ViewProfile:
    # Phase 7, Step 6E3B3 -- fixes a pre-existing defect found while
    # verifying this step's own simulation: _failed() never set
    # secret_policy_version, so it stayed at the dataclass default (0)
    # instead of the policy ACTIVE at compute time. classify_profile_state
    # compares that against current_secret_policy_version when a caller
    # supplies one (core.sqlservice.sql_validation always does) -- a
    # genuinely fresh TYPED_FAILURE profile was therefore misreported as
    # STALE by every such caller (both still block execution identically,
    # so this was never a safety gap, only a misleading diagnostic reason
    # string -- but it also corrupted this very milestone's own state-
    # count reporting until fixed here).
    return ViewProfile(
        object_fqn=object_fqn, source_id=source_id, build_id=build_id,
        schema_fingerprint=schema_fingerprint, definition_hash=definition_hash,
        profiling_version=VIEW_PROFILE_CONTRACT_VERSION, completion_status=status,
        failure_reason=reason, created_at=now, updated_at=now,
        definition_truncated_suspected=definition_truncated_suspected,
        evidence=(("definition_truncated_suspected",) if definition_truncated_suspected else ()),
        secret_policy_version=SECRET_POLICY_VERSION,
    )


# ── Scope resolution (real tables, CTEs, derived tables, CROSS/OUTER
#    APPLY -- all exposed uniformly as "a named source with known output
#    columns") ─────────────────────────────────────────────────────────


def _build_scopes(
    select: exp.Select,
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
    outer_scopes: Mapping[str, Mapping[str, "OutputColumnLineage"]],
    depth: int,
) -> tuple[dict[str, str], dict[str, Mapping[str, "OutputColumnLineage"]]]:
    """Returns (alias_map, scopes) for this SELECT's own FROM/JOIN clause.

    alias_map: alias_or_bare_name(lower) -> object_fqn(lower), for every
    REAL exp.Table source only (unchanged contract from the prior
    _build_alias_map) -- a table this module cannot FQN is simply absent.

    scopes: alias(lower) -> {column_name(lower): OutputColumnLineage}, for
    every CTE visible to this SELECT (inherited from `outer_scopes`, i.e.
    a WITH clause attached further out, or a sibling CTE already resolved
    earlier in the SAME WITH clause -- see compute_view_profile's own
    top-level WITH handling) PLUS every FROM/JOIN derived-table subquery
    and CROSS/OUTER APPLY target THIS select introduces, resolved
    recursively (Phase 7, Step 6E3B3, Part 2's own "outer aliases of CTE
    and derived-table outputs" / "alias propagation across multiple
    scopes" requirement). A local name shadows an outer one of the same
    name, matching real SQL scoping. Recursion is bounded by
    _MAX_SCOPE_RESOLUTION_DEPTH so a pathologically deep chain of nested
    derived tables can never prevent this module from terminating."""
    alias_map: dict[str, str] = {}
    local_scopes: dict[str, Mapping[str, OutputColumnLineage]] = {}

    from_clause = select.args.get("from_") or select.args.get("from")
    sources: list[exp.Expression] = []
    if from_clause is not None:
        sources.append(from_clause.this)
    for join in select.args.get("joins") or []:
        sources.append(join.this)

    for node in sources:
        target = node
        if isinstance(target, exp.Lateral):
            # CROSS/OUTER APPLY -- sqlglot wraps the correlated query as
            # Lateral(this=Subquery(this=Select|SetOperation) | Select),
            # alias on the Lateral node itself. Only resolved when the
            # AST actually exposes a Select/SetOperation (per this step's
            # own "where sqlglot exposes sufficient AST evidence"
            # allowance) -- any other shape (a table-valued function
            # call, e.g.) is simply absent, never guessed.
            alias = (target.alias_or_name or "").lower()
            inner = target.this
            if isinstance(inner, exp.Subquery):
                inner = inner.this
            if alias and isinstance(inner, (exp.Select, exp.SetOperation)) and depth < _MAX_SCOPE_RESOLUTION_DEPTH:
                local_scopes[alias] = _resolve_subquery_scope(
                    inner, known_columns_by_fqn_lower, outer_scopes, depth,
                )
            continue
        if isinstance(target, exp.Table):
            # Phase 7, Step 6E3B6, Part 3 -- a bare CTE reference
            # (`FROM cte_name` or `FROM cte_name alias`, no subquery
            # wrapper at all) parses as a plain exp.Table, indistinguishable
            # from a real table UNLESS checked against the CTE names
            # already visible here (outer_scopes). Found via this step's
            # own direct audit of a real view: its final SELECT references
            # exactly ONE of its WITH clause's four CTEs directly (`FROM
            # one_row_per_participant`); the other three are only used
            # internally, earlier in the CTE chain. Registering this target
            # into alias_map as if it were a real table (the prior
            # behavior) was harmless for a QUALIFIED reference (the scopes
            # check in _resolve_projection runs first and shadows it), but
            # a real correctness gap for an UNQUALIFIED one -- the proper
            # fix is in what key(s) end up in the RETURNED scopes dict
            # below, not here; this branch only needs to route a bare-CTE
            # target to local_scopes instead of alias_map.
            referenced_name_lower = (target.name or "").lower()
            if referenced_name_lower and referenced_name_lower in outer_scopes:
                alias = (target.alias_or_name or target.name or "").lower()
                if alias:
                    local_scopes[alias] = outer_scopes[referenced_name_lower]
                continue
            fqn = _authority._table_fqn(target)
            if not fqn:
                continue
            key = (target.alias_or_name or target.name or "").lower()
            if key:
                alias_map[key] = fqn.lower()
            continue
        if isinstance(target, exp.Subquery) and isinstance(target.this, (exp.Select, exp.SetOperation)):
            # Phase 7, Step 6E3B6, Part 3 -- a derived table's own body can
            # itself be a UNION (found via this step's own direct audit:
            # `(SELECT ... UNION ALL SELECT ... UNION ALL SELECT ...) AS
            # Subquery`) -- the prior isinstance(..., exp.Select)-only
            # check silently dropped this derived table entirely (never
            # registered as a scope at all), cascading into every outer
            # reference through its alias failing closed.
            alias = (target.alias_or_name or "").lower()
            if alias and depth < _MAX_SCOPE_RESOLUTION_DEPTH:
                local_scopes[alias] = _resolve_subquery_scope(
                    target.this, known_columns_by_fqn_lower, outer_scopes, depth,
                )
            continue
        # Any other FROM/JOIN source shape (a table-valued function call,
        # OPENQUERY/OPENROWSET, a bare subquery UNION, ...) is simply
        # absent from both maps -- callers already treat a missing alias
        # lookup as unresolved, never as "no table".

    # Phase 7, Step 6E3B6, Part 3 -- return ONLY the scopes this select's
    # OWN FROM/JOIN actually introduces (a real derived-table alias, a
    # CROSS/OUTER APPLY alias, or a bare CTE reference just resolved
    # above) -- NEVER every outer-sibling CTE outer_scopes happens to
    # carry forward for RECURSIVE resolution purposes. A real, valid SQL
    # qualified/unqualified reference at THIS select's own level can only
    # ever resolve through something THIS select's own FROM/JOIN clause
    # names; blindly re-exposing every sibling CTE here (the prior
    # behavior) made an UNQUALIFIED reference wrongly "ambiguous" against
    # CTEs that were never actually joined at this level at all (found via
    # this step's own direct audit: a 4-CTE chain where the final SELECT
    # only ever joins the last one). outer_scopes itself is still threaded
    # unchanged into _resolve_subquery_scope above, so a NESTED derived
    # table at a deeper level can still see every CTE visible to it by
    # name, exactly as real SQL scoping requires.
    return alias_map, local_scopes


def _resolve_union_or_select_lineage(
    tree: "exp.Select | exp.SetOperation",
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
    outer_scopes: Mapping[str, Mapping[str, "OutputColumnLineage"]],
    depth: int,
) -> list["OutputColumnLineage"]:
    """Phase 7, Step 6E3B6, Part 3 -- a derived table, CTE, or CROSS/
    OUTER APPLY body is not always a plain exp.Select; it can itself be a
    UNION/EXCEPT/INTERSECT (found via this step's own direct audit: a
    real view's CTE selects from `(SELECT ... UNION ALL SELECT ... UNION
    ALL SELECT ...) AS Subquery`). The prior code only ever accepted a
    plain exp.Select as a derived-table/CTE/APPLY body, silently dropping
    (never registering as a scope at all) any body shaped like this one
    -- the SAME per-branch flatten-and-merge logic compute_view_profile's
    own top-level UNION handling already uses, reused here so a nested
    UNION body is resolved identically regardless of how deep it is."""
    if isinstance(tree, exp.SetOperation):
        branches = _flatten_set_operation_branches(tree)
        per_branch_columns = [
            _resolve_select_lineage(branch, known_columns_by_fqn_lower, outer_scopes, depth + 1)
            for branch in branches
        ]
        return _merge_union_branch_columns(per_branch_columns)
    return _resolve_select_lineage(tree, known_columns_by_fqn_lower, outer_scopes, depth + 1)


def _merge_union_branch_columns(
    per_branch_columns: list[list["OutputColumnLineage"]],
) -> list["OutputColumnLineage"]:
    """Positionally merges each UNION branch's own resolved output columns
    into one list -- shared by compute_view_profile's top-level UNION
    handling and _resolve_union_or_select_lineage's nested-derived-table/
    CTE/APPLY case, so both resolve a UNION body identically."""
    output_columns: list[OutputColumnLineage] = []
    width = max((len(c) for c in per_branch_columns), default=0)
    _confidence_rank = {LINEAGE_CONFIDENCE_HIGH: 0, LINEAGE_CONFIDENCE_MEDIUM: 1, LINEAGE_CONFIDENCE_LOW: 2}
    for i in range(width):
        col_variants = [cols[i] for cols in per_branch_columns if i < len(cols)]
        if not col_variants:
            continue
        name = col_variants[0].output_name
        if any(c.confidence == LINEAGE_CONFIDENCE_UNRESOLVED for c in col_variants):
            unresolved_variants = [c for c in col_variants if c.confidence == LINEAGE_CONFIDENCE_UNRESOLVED]
            if any(c.candidate_source_fqns is None for c in unresolved_variants):
                merged_candidates = None
            else:
                merged_candidates = tuple(sorted(set().union(*(c.candidate_source_fqns or () for c in unresolved_variants))))
            output_columns.append(OutputColumnLineage(
                name, TRANSFORM_UNKNOWN, (), LINEAGE_CONFIDENCE_UNRESOLVED,
                LINEAGE_UNRESOLVED_UNION_BRANCH_MISMATCH, candidate_source_fqns=merged_candidates,
            ))
        else:
            merged_refs = tuple(dict.fromkeys(r for c in col_variants for r in c.source_refs))
            worst_confidence = max(
                (c.confidence for c in col_variants),
                key=lambda conf: _confidence_rank.get(conf, 2),
            )
            output_columns.append(OutputColumnLineage(name, col_variants[0].transformation_type, merged_refs, worst_confidence))
    return output_columns


def _resolve_subquery_scope(
    subquery_select: "exp.Select | exp.SetOperation",
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
    outer_scopes: Mapping[str, Mapping[str, "OutputColumnLineage"]],
    depth: int,
) -> dict[str, "OutputColumnLineage"]:
    """Resolve one derived-table/CTE/APPLY body's own output lineage so
    an outer query can treat its alias as a virtual table with known
    columns -- the SAME resolution pass this module applies to a real
    view's own outer query, applied recursively (now including a UNION-
    shaped body, Part 3). Never raises; an inner resolution failure
    simply yields UNRESOLVED columns in the returned scope, which
    _resolve_projection already treats as LINEAGE_UNRESOLVED_DEEP_CTE for
    an outer reference through it."""
    lineage = _resolve_union_or_select_lineage(subquery_select, known_columns_by_fqn_lower, outer_scopes, depth)
    return {col.output_name.lower(): col for col in lineage}


def _resolve_column_source(
    column: exp.Column, alias_map: Mapping[str, str],
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> tuple[tuple[str, str] | None, str | None, tuple[str, ...] | None]:
    """Returns ((object_fqn_lower, column_name), None, None) on success, or
    (None, unresolved_reason, candidate_source_fqns) on failure --
    candidate_source_fqns is the real, bounded set of tables this
    unresolved column could plausibly come from, when knowable (Phase 7,
    Step 6E3B5, Part 2 -- see OutputColumnLineage's own docstring for the
    None/()/non-empty contract). Never raises."""
    col_name = column.name
    if not col_name:
        return None, LINEAGE_UNRESOLVED_UNSUPPORTED_EXPR, ()
    qualifier = (column.table or "").lower()
    if qualifier:
        target = alias_map.get(qualifier)
        if target is None:
            # The qualifier doesn't match any registered real table OR
            # scope -- an unsupported/unregistered FROM source shape
            # (e.g. a table-valued function alias). Genuinely unknown,
            # never guessed down to "no risk".
            return None, LINEAGE_UNRESOLVED_UNKNOWN_ALIAS, None
        return (target, col_name), None, None

    # Unqualified column: disambiguate using each candidate source's own
    # known column names (already-persisted structural metadata) rather
    # than guessing -- if exactly one FROM/JOIN source actually has a
    # column with this name, that is a real, checkable fact, not a guess.
    candidates = [
        fqn for fqn in set(alias_map.values())
        if col_name.lower() in known_columns_by_fqn_lower.get(fqn, frozenset())
    ]
    if len(candidates) == 1:
        return (candidates[0], col_name), None, None
    if len(alias_map) == 1:
        # Only one source table at all (columns metadata simply absent) --
        # still a defensible resolution, but never upgraded past MEDIUM by
        # the caller.
        only = next(iter(alias_map.values()))
        return (only, col_name), None, None
    if not candidates:
        # None of the view's own FROM/JOIN tables has a column by this
        # name in our structural metadata -- it must still come from ONE
        # of those tables (SQL Server would reject the view otherwise),
        # so the real (bounded) candidate set is every FROM/JOIN table,
        # never the view's wider transitive dependency closure.
        return None, LINEAGE_UNRESOLVED_NO_MATCHING_COLUMN, tuple(sorted(set(alias_map.values())))
    return None, LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED, tuple(sorted(candidates))


def _projection_columns(node: exp.Expression) -> list[exp.Column]:
    prune = lambda n: isinstance(n, (exp.Subquery, exp.Select))
    out = []
    for item in node.walk(prune=prune):
        n = item[0] if isinstance(item, tuple) else item
        if isinstance(n, exp.Column):
            out.append(n)
    return out


def _has_nested_subquery(node: exp.Expression) -> bool:
    for item in node.walk():
        n = item[0] if isinstance(item, tuple) else item
        if n is not node and isinstance(n, (exp.Subquery, exp.Select)):
            return True
    return False


def _classify_projection(node: exp.Expression) -> str:
    prune = lambda n: isinstance(n, (exp.Subquery, exp.Select))
    for item in node.walk(prune=prune):
        n = item[0] if isinstance(item, tuple) else item
        if isinstance(n, exp.Window):
            return TRANSFORM_WINDOW
        if isinstance(n, exp.AggFunc):
            return TRANSFORM_AGGREGATE
        if isinstance(n, exp.Case):
            return TRANSFORM_CASE
    if isinstance(node, exp.Literal):
        return TRANSFORM_LITERAL
    if isinstance(node, exp.Column):
        return TRANSFORM_PASSTHROUGH
    return TRANSFORM_EXPRESSION


def _resolve_projection(
    projection: exp.Expression, alias_map: Mapping[str, str],
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
    scopes: Mapping[str, Mapping[str, "OutputColumnLineage"]],
) -> OutputColumnLineage:
    output_name = projection.alias_or_name or "?"
    expr = projection.this if isinstance(projection, exp.Alias) else projection

    if isinstance(expr, exp.Literal):
        return OutputColumnLineage(output_name, TRANSFORM_LITERAL, (), LINEAGE_CONFIDENCE_HIGH)

    if _has_nested_subquery(expr):
        return OutputColumnLineage(
            output_name, TRANSFORM_UNKNOWN, (), LINEAGE_CONFIDENCE_UNRESOLVED,
            LINEAGE_UNRESOLVED_NESTED_SUBQUERY, candidate_source_fqns=None,
        )

    transform_type = _classify_projection(expr)
    columns = _projection_columns(expr)
    if not columns:
        # No column reference anywhere in this expression (a pure
        # literal-like/function-only value sqlglot didn't classify as
        # exp.Literal) -- proven zero column reference, never a secret-
        # column passthrough.
        return OutputColumnLineage(
            output_name, transform_type, (), LINEAGE_CONFIDENCE_UNRESOLVED,
            LINEAGE_UNRESOLVED_UNSUPPORTED_EXPR, candidate_source_fqns=(),
        )

    resolved: list[tuple[str, str]] = []
    reasons: list[str] = []
    candidate_sets: list[tuple[str, ...] | None] = []
    for col in columns:
        qualifier = (col.table or "").lower()
        if qualifier in scopes:
            inner = scopes[qualifier].get(col.name.lower())
            if inner is None:
                reasons.append(LINEAGE_UNRESOLVED_DEEP_CTE)
                candidate_sets.append(None)
                continue
            if inner.confidence == LINEAGE_CONFIDENCE_UNRESOLVED:
                reasons.append(LINEAGE_UNRESOLVED_DEEP_CTE)
                candidate_sets.append(inner.candidate_source_fqns)
                continue
            resolved.extend(inner.source_refs)
            continue
        if not qualifier and scopes:
            # Phase 7, Step 6E3B6 -- an UNQUALIFIED column reference can
            # legitimately be satisfied by a derived-table/CTE scope, not
            # only a real FROM/JOIN table (e.g. "SELECT ... ListURL FROM
            # t LEFT JOIN (SELECT ..., ListURL FROM Details) d ON ...",
            # with the outer projection never qualifying "ListURL" at
            # all) -- found via this step's own live-verified audit of a
            # real view whose unqualified column was wrongly reported
            # "not found on any known source" despite being fully, simply
            # resolvable through exactly one derived table's own already-
            # resolved output. Disambiguate across REAL tables (by known
            # column names, as before) AND scopes (by the scope's own
            # already-resolved output column names) TOGETHER -- a real
            # fact, never a guess: if the column's name is only ever
            # satisfied by exactly one of these combined candidates, that
            # is the same kind of checkable certainty the real-table-only
            # path already relied on.
            name_lower = col.name.lower()
            scope_matches = [s for s in scopes if name_lower in scopes[s]]
            table_matches = [
                fqn for fqn in set(alias_map.values())
                if name_lower in known_columns_by_fqn_lower.get(fqn, frozenset())
            ]
            total = len(scope_matches) + len(table_matches)
            if total == 1:
                if scope_matches:
                    inner = scopes[scope_matches[0]][name_lower]
                    if inner.confidence == LINEAGE_CONFIDENCE_UNRESOLVED:
                        reasons.append(LINEAGE_UNRESOLVED_DEEP_CTE)
                        candidate_sets.append(inner.candidate_source_fqns)
                        continue
                    resolved.extend(inner.source_refs)
                    continue
                resolved.append((table_matches[0], col.name))
                continue
            if total > 1:
                reasons.append(LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED)
                candidate_sets.append(tuple(sorted(set(table_matches) | set(scope_matches))))
                continue
            # total == 0 for the combined (table ∪ scope) search too --
            # fall through to _resolve_column_source's own real-table-only
            # fallback paths below (including its "only one FROM/JOIN
            # table at all" defensible-resolution case), unchanged.
        source, reason, candidates = _resolve_column_source(col, alias_map, known_columns_by_fqn_lower)
        if source is None:
            reasons.append(reason or LINEAGE_UNRESOLVED_UNSUPPORTED_EXPR)
            candidate_sets.append(candidates)
            continue
        resolved.append(source)

    if reasons:
        # Merge every failing sub-column's own candidate set -- if ANY one
        # of them is opaque (None), the whole projection's candidate set
        # must also be treated as opaque (never silently drop a genuinely
        # unknown candidate just because a SIBLING column resolved fine).
        if any(c is None for c in candidate_sets):
            merged_candidates = None
        else:
            merged_candidates = tuple(sorted(set().union(*candidate_sets))) if candidate_sets else ()
        return OutputColumnLineage(
            output_name, transform_type, tuple(dict.fromkeys(resolved))[:_MAX_LIST_FIELD],
            LINEAGE_CONFIDENCE_UNRESOLVED, reasons[0], candidate_source_fqns=merged_candidates,
        )

    if transform_type == TRANSFORM_PASSTHROUGH and len(resolved) == 1 and (alias_map or scopes):
        confidence = LINEAGE_CONFIDENCE_HIGH if resolved[0][0] in alias_map.values() else LINEAGE_CONFIDENCE_MEDIUM
    elif transform_type in (TRANSFORM_AGGREGATE, TRANSFORM_CASE, TRANSFORM_EXPRESSION, TRANSFORM_WINDOW):
        confidence = LINEAGE_CONFIDENCE_MEDIUM
    else:
        confidence = LINEAGE_CONFIDENCE_LOW

    return OutputColumnLineage(
        output_name, transform_type, tuple(dict.fromkeys(resolved))[:_MAX_LIST_FIELD], confidence,
    )


def _expand_star(
    qualifier: str | None,
    alias_map: Mapping[str, str],
    scopes: Mapping[str, Mapping[str, "OutputColumnLineage"]],
    known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> list[OutputColumnLineage]:
    """Phase 7, Step 6E3B3, Part 2/4 -- safe `SELECT *` / `alias.*`
    expansion from stored structural metadata. Every FROM/JOIN/scope
    target (unqualified star) or just the one named target (qualified
    `alias.*`) is expanded, each column positionally attributed to its
    OWN real source in FROM/JOIN declaration order -- this is never a
    name-matching guess (a column's source here is decided purely by
    which table/scope actually projects it, never by comparing names
    across candidates), matching real SQL Server `SELECT *` semantics: a
    duplicate-named column from two different sources is legitimately two
    separate output columns, each still individually resolved."""
    if qualifier:
        if qualifier in scopes:
            cols = scopes[qualifier]
            return [
                OutputColumnLineage(
                    c.output_name, TRANSFORM_STAR_EXPANSION, c.source_refs, c.confidence, c.unresolved_reason,
                    candidate_source_fqns=c.candidate_source_fqns,
                )
                for c in sorted(cols.values(), key=lambda c: c.output_name)
            ][:_MAX_OUTPUT_COLUMNS]
        target_fqn = alias_map.get(qualifier)
        if target_fqn is None:
            return [OutputColumnLineage(
                "*", TRANSFORM_STAR_EXPANSION, (), LINEAGE_CONFIDENCE_UNRESOLVED,
                LINEAGE_UNRESOLVED_UNKNOWN_ALIAS, candidate_source_fqns=None,
            )]
        ordered_targets: list[tuple[str, str | None]] = [(target_fqn, None)]
    else:
        # Unqualified `*` -- every real table in FROM/JOIN declaration
        # order, followed by every scope (derived table/CTE/APPLY) in
        # declaration order. dict preserves insertion order (Python 3.7+),
        # and alias_map/scopes were both built by iterating FROM/JOIN in
        # order, so this reproduces SQL Server's own left-to-right column
        # ordering.
        seen_fqns: set[str] = set()
        ordered_targets = []
        for fqn in alias_map.values():
            if fqn not in seen_fqns:
                seen_fqns.add(fqn)
                ordered_targets.append((fqn, None))
        for scope_name in scopes:
            ordered_targets.append((None, scope_name))

    if not ordered_targets:
        return [OutputColumnLineage(
            "*", TRANSFORM_STAR_EXPANSION, (), LINEAGE_CONFIDENCE_UNRESOLVED,
            LINEAGE_UNRESOLVED_AMBIGUOUS_STAR, candidate_source_fqns=(),
        )]

    out: list[OutputColumnLineage] = []
    for fqn, scope_name in ordered_targets:
        if scope_name is not None:
            for c in sorted(scopes[scope_name].values(), key=lambda c: c.output_name):
                out.append(OutputColumnLineage(
                    c.output_name, TRANSFORM_STAR_EXPANSION, c.source_refs, c.confidence, c.unresolved_reason,
                    candidate_source_fqns=c.candidate_source_fqns,
                ))
            continue
        columns = sorted(known_columns_by_fqn_lower.get(fqn, frozenset()))
        for col in columns:
            out.append(OutputColumnLineage(col, TRANSFORM_STAR_EXPANSION, ((fqn, col),), LINEAGE_CONFIDENCE_MEDIUM))

    if not out:
        real_targets = tuple(sorted({fqn for fqn, scope_name in ordered_targets if fqn is not None}))
        return [OutputColumnLineage(
            "*", TRANSFORM_STAR_EXPANSION, (), LINEAGE_CONFIDENCE_UNRESOLVED,
            LINEAGE_UNRESOLVED_NO_MATCHING_COLUMN, candidate_source_fqns=real_targets,
        )]
    return out[:_MAX_OUTPUT_COLUMNS]


def _resolve_select_lineage(
    select: exp.Select, known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
    outer_scopes: Mapping[str, Mapping[str, OutputColumnLineage]],
    depth: int = 0,
) -> list[OutputColumnLineage]:
    alias_map, scopes = _build_scopes(select, known_columns_by_fqn_lower, outer_scopes, depth)
    out: list[OutputColumnLineage] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            out.extend(_expand_star(None, alias_map, scopes, known_columns_by_fqn_lower))
            continue
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            qualifier = (projection.table or "").lower() or None
            out.extend(_expand_star(qualifier, alias_map, scopes, known_columns_by_fqn_lower))
            continue
        out.append(_resolve_projection(projection, alias_map, known_columns_by_fqn_lower, scopes))
    return out[:_MAX_OUTPUT_COLUMNS]


def _resolve_with_clause_scopes(
    tree: "exp.Select | exp.SetOperation", known_columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> dict[str, dict[str, OutputColumnLineage]]:
    """Resolves every CTE in `tree`'s own WITH clause, in declaration
    order, each one visible to every CTE declared after it in the SAME
    WITH clause (real SQL Server CTE scoping) -- "nested queries and CTEs
    WHERE SUPPORTED" per this milestone's own explicit allowance for
    partial support. A CTE that itself references a derived table/APPLY
    is resolved the same way any other SELECT would be (see
    _build_scopes); a CTE reference that still can't be resolved (e.g. a
    PARSE-opaque construct inside it) surfaces as LINEAGE_UNRESOLVED_DEEP_
    CTE at the point an outer query actually references that column,
    never guessed."""
    with_clause = tree.args.get("with_") or tree.args.get("with")
    if with_clause is None:
        return {}
    result: dict[str, dict[str, OutputColumnLineage]] = {}
    for cte in with_clause.expressions:
        if not isinstance(cte, exp.CTE):
            continue
        name = (cte.alias_or_name or "").lower()
        inner = cte.this
        if not isinstance(inner, (exp.Select, exp.SetOperation)):
            continue
        lineage = _resolve_union_or_select_lineage(inner, known_columns_by_fqn_lower, result, 0)
        result[name] = {col.output_name.lower(): col for col in lineage}
    return result


def _flatten_set_operation_branches(tree: exp.SetOperation) -> list[exp.Select]:
    """Every exp.Select leaf of a (possibly 3+-way) UNION/EXCEPT/INTERSECT
    chain, left-to-right. sqlglot represents `A UNION B UNION C` as
    SetOperation(left=SetOperation(left=A, right=B), right=C) -- a naive
    `(tree.left, tree.right)` read only ever sees SetOperation(A,B) and C,
    silently losing A and B. Bounded by _MAX_OUTPUT_COLUMNS worth of
    recursion depth in practice (a real view never nests this deep), but
    unconditionally bounded here too so a pathological input can never
    cause unbounded recursion."""
    branches: list[exp.Select] = []

    def _walk(node: exp.Expression, depth: int) -> None:
        if depth > _MAX_SCOPE_RESOLUTION_DEPTH * 4:
            return
        if isinstance(node, exp.Subquery):
            _walk(node.this, depth + 1)
        elif isinstance(node, exp.SetOperation):
            _walk(node.left, depth + 1)
            _walk(node.right, depth + 1)
        elif isinstance(node, exp.Select):
            branches.append(node)
        # any other branch shape (e.g. a Subquery wrapper) is simply
        # absent -- the branch-count/branch-mismatch logic in
        # compute_view_profile already treats a missing branch as reduced
        # coverage, never a silent guess.

    _walk(tree, 0)
    return branches


_SPLIT_OPERATOR_PAIRS = {("<", "="): "<=", (">", "="): ">=", ("!", "="): "!=", ("<", ">"): "<>"}


def _normalize_split_comparison_operators(sql: str) -> str:
    """Phase 7, Step 6E3B5, Part 4 -- SQL Server's own tokenizer tolerates
    whitespace between the two characters of a multi-character comparison
    operator (`> =`, `< =`, `! =`, `< >`) -- confirmed directly: both of
    this catalog's remaining PARSE_ERROR views fail with the identical
    sqlglot error shape ("Required keyword: 'expression' missing for
    <... GT/LT>") at a line containing exactly this pattern. sqlglot's own
    T-SQL tokenizer does not tolerate the whitespace. This collapses
    exactly those four operator pairs when whitespace(-only, same-line)
    separated, but ONLY outside a single-quoted string literal (doubled
    `''` handled as an escaped quote), a bracketed `[identifier]`, a
    `--` line comment, or a `/* */` block comment -- a value or identifier
    that happens to CONTAIN "> =" as literal text is never touched. Pure
    text transform; never changes the SQL's own meaning for any
    syntactically valid construct, only makes an already SQL-Server-valid
    one ALSO valid to this parser. Applied only as a fallback retry after
    the unmodified text fails to parse -- never applied to, or allowed to
    silently change the meaning of, SQL that already parsed successfully."""
    out: list[str] = []
    i, n = 0, len(sql)
    in_single_quote = in_bracket = in_line_comment = in_block_comment = False
    while i < n:
        ch = sql[i]
        if in_line_comment:
            out.append(ch)
            i += 1
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and i + 1 < n and sql[i + 1] == "/":
                out.append("*/")
                i += 2
                in_block_comment = False
            else:
                out.append(ch)
                i += 1
            continue
        if in_single_quote:
            if ch == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("''")
                    i += 2
                    continue
                in_single_quote = False
            out.append(ch)
            i += 1
            continue
        if in_bracket:
            out.append(ch)
            i += 1
            if ch == "]":
                in_bracket = False
            continue
        if ch == "'":
            in_single_quote = True
            out.append(ch)
            i += 1
            continue
        if ch == "[":
            in_bracket = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            in_line_comment = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            in_block_comment = True
            out.append(ch)
            i += 1
            continue
        if ch in "<>!":
            j = i + 1
            while j < n and sql[j] in " \t":
                j += 1
            if j > i + 1 and j < n and sql[j] in "=>" and (ch, sql[j]) in _SPLIT_OPERATOR_PAIRS:
                out.append(_SPLIT_OPERATOR_PAIRS[(ch, sql[j])])
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# ── Main entry point ─────────────────────────────────────────────────────


def compute_view_profile(
    object_fqn: str,
    definition_sql: str | None,
    *,
    source_id: int,
    build_id: int | None,
    schema_fingerprint: str | None,
    direct_dependencies: Sequence[str],
    transitive_dependencies: Sequence[str],
    authority: "_authority.SchemaObjectAuthority | None",
    column_names_by_fqn_lower: Mapping[str, frozenset[str]],
    primary_key_columns_by_fqn_lower: Mapping[str, tuple[str, ...]],
    is_framework_backed_hint: bool = False,
    now: str = "",
    definition_capture_status: str | None = None,
) -> ViewProfile:
    """`definition_capture_status`, when supplied (Phase 7, Step 6E3B4,
    Part 2/3), is the UPSTREAM capture layer's own authoritative verdict
    (data.view_semantics_service.CAPTURE_STATUS_*) on whether
    definition_sql is complete. "AVAILABLE_COMPLETE" overrides this
    module's own length-based truncation HEURISTIC outright (a genuinely
    complete definition that happens to land at exactly
    _SUSPECTED_TRUNCATION_LENGTH characters must never be flagged
    truncated just because of its length) -- any other value, or None
    (the legacy per-view capture path, which cannot make this guarantee),
    leaves the heuristic in force unchanged."""
    definition_hash = (
        hashlib.sha256(definition_sql.encode("utf-8")).hexdigest() if definition_sql else None
    )
    if not definition_sql or not definition_sql.strip():
        return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                        COMPLETION_FAILED, FAILURE_NO_DEFINITION, now)
    capture_confirmed_complete = definition_capture_status == "AVAILABLE_COMPLETE"

    # Phase 7, Step 6E3B3 -- see this module's own docstring addendum: a
    # definition_sql at EXACTLY this length is suspected truncated by the
    # capture path, never confirmed complete regardless of whether it
    # happens to parse below. Phase 7, Step 6E3B4, Part 2/3 -- EXCEPT when
    # the upstream capture layer has already authoritatively confirmed
    # completeness (capture_confirmed_complete) -- that positive evidence
    # always wins over this length-based guess.
    definition_truncated_suspected = (
        not capture_confirmed_complete and len(definition_sql) == _SUSPECTED_TRUNCATION_LENGTH
    )

    operator_normalization_applied = False
    parse_sql = definition_sql
    try:
        statements = [s for s in sqlglot.parse(parse_sql, read=TSQL_DIALECT) if s is not None]
    except Exception:  # noqa: BLE001 -- first attempt failed; try one safe, generic, bounded preprocessing fallback below before giving up
        normalized = _normalize_split_comparison_operators(definition_sql)
        if normalized == definition_sql:
            return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                            COMPLETION_FAILED, FAILURE_PARSE_ERROR, now,
                            definition_truncated_suspected=definition_truncated_suspected)
        try:
            statements = [s for s in sqlglot.parse(normalized, read=TSQL_DIALECT) if s is not None]
        except Exception:  # noqa: BLE001 -- still unparseable even after normalization; a typed, final failure
            return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                            COMPLETION_FAILED, FAILURE_PARSE_ERROR, now,
                            definition_truncated_suspected=definition_truncated_suspected)
        parse_sql = normalized
        operator_normalization_applied = True
    if len(statements) != 1:
        # Phase 7, Step 6E3B3, Part 2 -- "common SQL Server CREATE VIEW
        # wrappers and session statements": a stored definition_sql that
        # (unusually) still carries a leading/trailing session statement
        # (SET ANSI_NULLS ON, SET QUOTED_IDENTIFIER ON, ...) parses as
        # MULTIPLE top-level statements: only the ones that are a real
        # CREATE/SELECT/set-operation ever carry the view's own body.
        # Never guessed when that does not leave exactly one candidate.
        body_statements = [
            s for s in statements if isinstance(s, (exp.Create, exp.Select, exp.SetOperation))
        ]
        if len(body_statements) != 1:
            return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                            COMPLETION_FAILED, FAILURE_PARSE_ERROR, now,
                            definition_truncated_suspected=definition_truncated_suspected)
        statements = body_statements

    tree = statements[0]
    if isinstance(tree, exp.Create):
        tree = tree.expression
    while isinstance(tree, exp.Subquery):
        tree = tree.this
    if tree is None:
        return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                        COMPLETION_FAILED, FAILURE_PARSE_ERROR, now,
                        definition_truncated_suspected=definition_truncated_suspected)

    ast_facts = _authority._derive_ast_facts(parse_sql)
    if ast_facts.parse_error is not None:
        return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                        COMPLETION_FAILED, FAILURE_PARSE_ERROR, now,
                        definition_truncated_suspected=definition_truncated_suspected)

    has_union = isinstance(tree, exp.SetOperation)
    output_columns: list[OutputColumnLineage] = []
    group_by_columns: list[str] = []
    aggregate_functions: list[str] = []
    window_functions: list[str] = []
    where_restrictions: list[str] = []
    having_restrictions: list[str] = []
    union_branch_count = 0

    if has_union:
        # Phase 7, Step 6E3B3 -- flatten a 3+-way UNION chain. sqlglot
        # nests these as SetOperation(SetOperation(A, B), C): the prior
        # implementation only ever inspected tree.left/tree.right
        # directly, silently dropping every branch beyond the first two
        # whenever tree.left was itself a SetOperation (a latent
        # correctness risk, not just a coverage gap -- lineage could
        # otherwise be computed from an incomplete branch set without any
        # typed signal that branches were dropped).
        #
        # Phase 7, Step 6E3B6, Part 3 -- a second, more severe bug found
        # via this step's own direct audit: a `WITH cte AS (...) SELECT
        # ... UNION ALL SELECT ...` attaches its WITH clause to the TOP-
        # LEVEL SetOperation node, NEVER to either branch (confirmed
        # directly against sqlglot's own AST: `tree.args["with_"]` is
        # populated, `tree.left.args["with_"]` is None). The prior code
        # called _resolve_with_clause_scopes(branch, ...) PER BRANCH,
        # which could only ever find a with_ clause that doesn't exist
        # there -- every CTE a UNION-shaped view's branches depend on was
        # silently invisible to all of them. Resolved ONCE here, from the
        # real top-level node, and threaded into every branch as its own
        # outer_scopes -- exactly how a plain (non-union) view's own CTEs
        # already worked.
        union_cte_lineage = _resolve_with_clause_scopes(tree, column_names_by_fqn_lower)
        branches = _flatten_set_operation_branches(tree)
        union_branch_count = len(branches)
        per_branch_columns: list[list[OutputColumnLineage]] = []
        for branch in branches:
            per_branch_columns.append(_resolve_select_lineage(branch, column_names_by_fqn_lower, union_cte_lineage))
            for agg in _authority._root_level_aggregate_functions(branch):
                aggregate_functions.append(type(agg).__name__.upper())
        output_columns = _merge_union_branch_columns(per_branch_columns)
    else:
        if not isinstance(tree, exp.Select):
            return _failed(object_fqn, source_id, build_id, schema_fingerprint, definition_hash,
                            COMPLETION_FAILED, FAILURE_UNSUPPORTED_STATEMENT, now,
                            definition_truncated_suspected=definition_truncated_suspected)
        cte_lineage = _resolve_with_clause_scopes(tree, column_names_by_fqn_lower)
        output_columns = _resolve_select_lineage(tree, column_names_by_fqn_lower, cte_lineage)
        for agg in _authority._root_level_aggregate_functions(tree):
            aggregate_functions.append(type(agg).__name__.upper())
        group_clause = tree.args.get("group")
        if group_clause is not None:
            group_by_columns = [str(e.name or e.sql(dialect=TSQL_DIALECT)) for e in group_clause.expressions][:_MAX_LIST_FIELD]
        for item in tree.walk(prune=lambda n: _authority._prune_nested_queries(n, tree)):
            n = item[0] if isinstance(item, tuple) else item
            if isinstance(n, exp.Window):
                func = n.this
                window_functions.append(type(func).__name__.upper() if func is not None else "WINDOW")
        if tree.args.get("where") is not None:
            where_restrictions.append(tree.args["where"].this.sql(dialect=TSQL_DIALECT))
        if tree.args.get("having") is not None:
            having_restrictions.append(tree.args["having"].this.sql(dialect=TSQL_DIALECT))

    lineage_complete = bool(output_columns) and all(
        c.confidence != LINEAGE_CONFIDENCE_UNRESOLVED for c in output_columns
    )

    join_kinds = sorted(set(ast_facts.join_kind_by_table_fqn_lower.values()))
    inner_join_count = sum(
        1 for fqn, kind in ast_facts.join_kind_by_table_fqn_lower.items()
        if kind not in ("LEFT", "FULL") and fqn != ast_facts.anchor_table_fqn_lower
    )
    if inner_join_count == 0:
        duplication_risk = DUPLICATION_RISK_NONE
    elif inner_join_count == 1:
        duplication_risk = DUPLICATION_RISK_POSSIBLE
    else:
        duplication_risk = DUPLICATION_RISK_LIKELY

    if ast_facts.has_group_by or ast_facts.has_root_level_aggregate:
        grain_class = _authority.GRAIN_CLASS_AGGREGATED
    elif ast_facts.has_distinct:
        grain_class = _authority.GRAIN_CLASS_DEDUPLICATED
    else:
        grain_class = _authority.GRAIN_CLASS_BASE_ROW
    grain_confidence = LINEAGE_CONFIDENCE_HIGH if ast_facts.parse_error is None else LINEAGE_CONFIDENCE_UNRESOLVED

    if ast_facts.has_literal_comparison_filter:
        row_preservation_risk = _authority.ROW_PRESERVATION_REDUCED
    elif inner_join_count > 0:
        row_preservation_risk = _authority.ROW_PRESERVATION_AT_RISK
    else:
        row_preservation_risk = _authority.ROW_PRESERVATION_PRESERVED

    # Entity key: prefer a reached-canonical table's own primary key
    # column when it is actually present among this view's own resolved
    # output columns -- the same "required canonical columns... remain
    # available" bar schema_object_authority's PROVEN_EQUIVALENT_VIEW
    # class already enforces, reused here for suitability rather than
    # duplicated with different logic.
    entity_keys: list[str] = []
    required_distinct_key: str | None = None
    all_dep_fqns_lower = {d.lower() for d in direct_dependencies} | {d.lower() for d in transitive_dependencies}
    if ast_facts.anchor_table_fqn_lower:
        all_dep_fqns_lower.add(ast_facts.anchor_table_fqn_lower)
    for dep_fqn in all_dep_fqns_lower:
        for pk_col in primary_key_columns_by_fqn_lower.get(dep_fqn, ()):
            for out_col in output_columns:
                if out_col.confidence == LINEAGE_CONFIDENCE_UNRESOLVED:
                    continue
                if any(ref[0] == dep_fqn and ref[1].lower() == pk_col.lower() for ref in out_col.source_refs):
                    entity_keys.append(out_col.output_name)
                    if required_distinct_key is None:
                        required_distinct_key = out_col.output_name
    entity_keys = sorted(set(entity_keys))[:_MAX_LIST_FIELD]

    authority_class = authority.authority_class if authority is not None else _authority.AUTHORITY_CLASS_UNKNOWN
    is_framework_backed = bool(is_framework_backed_hint)

    population_restrictions = tuple(where_restrictions) + tuple(having_restrictions)

    unsuitable_reasons: list[str] = []
    suitable_for_detail = grain_class == _authority.GRAIN_CLASS_BASE_ROW and row_preservation_risk != _authority.ROW_PRESERVATION_REDUCED
    if not suitable_for_detail:
        unsuitable_reasons.append(f"{PURPOSE_DETAIL}:aggregated_or_reduced_grain_no_row_detail")

    suitable_for_count = bool(entity_keys) or grain_class != _authority.GRAIN_CLASS_AGGREGATED
    if duplication_risk in (DUPLICATION_RISK_POSSIBLE, DUPLICATION_RISK_LIKELY) and not required_distinct_key:
        unsuitable_reasons.append(f"{PURPOSE_COUNT}:fanout_risk_no_resolvable_distinct_key")

    suitable_for_aggregation = row_preservation_risk != _authority.ROW_PRESERVATION_REDUCED and duplication_risk != DUPLICATION_RISK_LIKELY
    if not suitable_for_aggregation:
        unsuitable_reasons.append(f"{PURPOSE_AGGREGATION}:row_preservation_or_fanout_risk")

    suitable_for_join = row_preservation_risk == _authority.ROW_PRESERVATION_PRESERVED and bool(entity_keys)
    if not suitable_for_join:
        unsuitable_reasons.append(f"{PURPOSE_JOIN}:no_stable_preserved_entity_key")

    suitability = ViewSuitability(
        suitable_for_detail=suitable_for_detail,
        suitable_for_count=suitable_for_count,
        suitable_for_aggregation=suitable_for_aggregation,
        suitable_for_join=suitable_for_join,
        unsuitable_reasons=tuple(unsuitable_reasons),
        required_distinct_key=required_distinct_key,
        population_restriction=(population_restrictions[0] if population_restrictions else None),
        confidence=grain_confidence,
    )

    pii_categories: list[str] = []
    secret_columns: list[str] = []
    for out_col in output_columns:
        name = out_col.output_name
        if not name or name == "*":
            continue
        _is_pii, tier, _evidence = detect_pii_tier(name, "TEXT")
        if tier > 0:
            pii_categories.append(f"{name}:TIER{tier}")
        # Phase 7, Step 6E3B5, Part 2 -- check the REAL source column
        # name(s) this output is resolved FROM (source_refs), not only its
        # own output alias. "SELECT PasswordHash AS Foo" must still be
        # caught (the alias "Foo" alone would miss it); an innocuously-
        # named passthrough of a genuinely restricted source column is the
        # actual Category-A exposure this field exists to prove -- the
        # output alias is only ever a SECONDARY signal, never the only one.
        if is_restricted_category(name) or any(is_restricted_category(src_col) for _src_fqn, src_col in out_col.source_refs):
            secret_columns.append(name)

    snapshot_status = (
        SNAPSHOT_STATUS_SNAPSHOT_OR_STALE
        if authority is not None and authority.object_fqn.lower() in (authority.unsafe_dependency_fqns or ())
        else SNAPSHOT_STATUS_CURRENT
    )

    evidence: list[str] = [f"derived_from_definition_hash:{definition_hash[:12] if definition_hash else 'none'}"]
    if has_union:
        evidence.append(f"union_branches:{union_branch_count}")
    if not lineage_complete:
        evidence.append("lineage_incomplete:one_or_more_output_columns_unresolved")
    if definition_truncated_suspected:
        evidence.append("definition_truncated_suspected")
    if operator_normalization_applied:
        evidence.append("parsed_after_split_comparison_operator_normalization")

    completion_status = COMPLETION_COMPLETE if lineage_complete else COMPLETION_INCOMPLETE
    failure_reason = None if lineage_complete else "LINEAGE_INCOMPLETE"

    # Phase 7, Step 6E3B3, Part 3/4 -- LIMITED_COMPLETE eligibility. Only
    # ever evaluated for an otherwise-INCOMPLETE profile (a COMPLETE
    # profile needs no fallback; a FAILED one already returned above with
    # no output_columns to evaluate at all). Fails closed: ANY one of
    # these disqualifiers blocks eligibility outright, and this bar is
    # re-evaluated from scratch on every recomputation -- there is no
    # "trust it was eligible before" shortcut.
    limited_complete_reasons: list[str] = []
    if completion_status != COMPLETION_INCOMPLETE:
        limited_complete_reasons.append("not_applicable_completion_status_not_incomplete")
    if definition_truncated_suspected:
        limited_complete_reasons.append("definition_truncated_suspected")
    if snapshot_status != SNAPSHOT_STATUS_CURRENT:
        limited_complete_reasons.append("snapshot_or_stale_dependency")
    if secret_columns:
        limited_complete_reasons.append("own_output_projects_secret_column")
    # Phase 7, Step 6E3B5, Part 2 -- "no referenced dependency contains a
    # secret column that might be projected UNLESS the profiler can prove
    # that column is not exposed". The PRIOR implementation (Step 6E3B3)
    # proved this far too bluntly: it flagged risk whenever ANY table in
    # the view's ENTIRE transitive dependency closure carried ANY secret-
    # shaped column anywhere, regardless of whether that table had
    # anything to do with the view's actual unresolved output columns --
    # a view depending (even transitively, through another view) on a
    # framework identity table was penalized even when every one of its
    # own output columns was provably unrelated to that table. The real,
    # narrower question is: for EACH still-unresolved output column, is a
    # secret-bearing table actually among the BOUNDED, SPECIFIC candidate
    # set that column could plausibly come from (OutputColumnLineage.
    # candidate_source_fqns, Part 2's own new field)? `None` (a genuinely
    # opaque column -- a scalar subquery, an unregistered alias, ...)
    # still can never be proven safe and conservatively counts as risk;
    # `()` (proven zero column reference) can never carry a secret.
    # Category-C views (a secret-bearing table is only a DEPENDENCY, never
    # a candidate source for any actually-unresolved column) no longer
    # trip this check at all -- an internal framework dependency alone
    # must never make an otherwise-clean business view unsafe.
    dependency_secret_exposure_risk = False
    for out_col in output_columns:
        if out_col.confidence != LINEAGE_CONFIDENCE_UNRESOLVED:
            continue
        candidates = out_col.candidate_source_fqns
        # None (genuinely opaque -- can't bound the candidate set at all)
        # still falls back to the view's FULL dependency closure: if NO
        # table anywhere in it carries a secret-shaped column, there is
        # nothing even an opaque column could be exposing, regardless of
        # how little is known about its specific source. Only an opaque
        # column WHOSE closure actually contains a secret-shaped table is
        # unprovable risk.
        check_set = all_dep_fqns_lower if candidates is None else candidates
        if any(is_restricted_category(col) for fqn in check_set for col in column_names_by_fqn_lower.get(fqn, frozenset())):
            dependency_secret_exposure_risk = True
            break
    if dependency_secret_exposure_risk:
        limited_complete_reasons.append("unresolved_dependency_secret_exposure_risk")
    resolved_output_count = sum(1 for c in output_columns if c.confidence != LINEAGE_CONFIDENCE_UNRESOLVED)
    resolved_output_ratio = (resolved_output_count / len(output_columns)) if output_columns else 0.0
    if not output_columns or resolved_output_ratio < _LIMITED_COMPLETE_MIN_RESOLVED_RATIO:
        limited_complete_reasons.append("insufficient_resolved_output_column_ratio")

    limited_complete_eligible = not limited_complete_reasons
    if limited_complete_eligible:
        evidence.append("limited_complete_eligible:catalog_confirmed_output_no_secret_exposure")

    return ViewProfile(
        object_fqn=object_fqn, source_id=source_id, build_id=build_id,
        schema_fingerprint=schema_fingerprint, definition_hash=definition_hash,
        profiling_version=VIEW_PROFILE_CONTRACT_VERSION,
        completion_status=completion_status, failure_reason=failure_reason,
        output_columns=tuple(output_columns),
        direct_dependencies=tuple(sorted(set(direct_dependencies)))[:_MAX_LIST_FIELD],
        transitive_dependencies=tuple(sorted(set(transitive_dependencies)))[:_MAX_LIST_FIELD],
        join_types=tuple(join_kinds),
        where_restrictions=tuple(where_restrictions),
        having_restrictions=tuple(having_restrictions),
        literal_projections=ast_facts.literal_projection_aliases,
        has_distinct=ast_facts.has_distinct,
        group_by_columns=tuple(group_by_columns),
        aggregate_functions=tuple(sorted(set(aggregate_functions))),
        has_union=has_union, union_branch_count=union_branch_count,
        window_functions=tuple(sorted(set(window_functions))),
        has_top_limit=ast_facts.has_row_limit,
        grain_class=grain_class, grain_confidence=grain_confidence,
        entity_keys=tuple(entity_keys), required_distinct_key=required_distinct_key,
        duplication_risk=duplication_risk, row_preservation_risk=row_preservation_risk,
        population_restrictions=population_restrictions,
        suitability=suitability,
        authority_class=authority_class, is_framework_backed=is_framework_backed,
        lineage_complete=lineage_complete,
        pii_categories=tuple(pii_categories)[:_MAX_LIST_FIELD],
        secret_columns=tuple(sorted(set(secret_columns)))[:_MAX_LIST_FIELD],
        snapshot_status=snapshot_status, cross_source_status=CROSS_SOURCE_STATUS_SINGLE_SOURCE,
        evidence=tuple(evidence), created_at=now, updated_at=now,
        secret_policy_version=SECRET_POLICY_VERSION,
        definition_truncated_suspected=definition_truncated_suspected,
        limited_complete_eligible=limited_complete_eligible,
        limited_complete_reasons=tuple(limited_complete_reasons),
    )


def classify_profile_state(
    profile: ViewProfile | None, *, schema_fingerprint: str | None,
    profiling_version: int = VIEW_PROFILE_CONTRACT_VERSION,
    current_definition_hash: str | None = None,
    current_secret_policy_version: int | None = None,
) -> str:
    """The single source of truth for "can this persisted profile be
    trusted for THIS build" (Phase 7, Step 6E3B1 Part 2; freshness
    widened in Step 6E3B2 Part 1 to also cover definition-hash drift
    between builds and secret/PII policy currency).

    PROFILE_STATE_MISSING: no row at all -- a brand-new view, or one the
        bootstrap has not reached yet (see data.view_profile_service's own
        PARTIAL-status contract).
    PROFILE_STATE_STALE: a row exists but was computed for a DIFFERENT
        schema_fingerprint (the definition/schema changed since), under a
        since-superseded VIEW_PROFILE_CONTRACT_VERSION, against a view
        definition that has since changed (current_definition_hash, when
        supplied, differs from the profile's own definition_hash -- this
        can drift independently of schema_fingerprint if a view's SQL
        Server definition is edited between full retrieval rebuilds), or
        under a since-superseded secret/PII policy (current_secret_
        policy_version, when supplied, differs from the profile's own) --
        never trusted, regardless of its own completion_status.
    PROFILE_STATE_TYPED_FAILURE: fresh for this build, but the profiler
        could not parse/resolve the definition at all (COMPLETION_FAILED).
    PROFILE_STATE_UNSAFE: fresh for this build, parsed, INCOMPLETE, and
        would otherwise be a LINEAGE_INCOMPLETE candidate -- but carries a
        KNOWN or unprovable secret-exposure risk (Phase 7, Step 6E3B3,
        Part 3/4: profile.limited_complete_reasons contains
        "own_output_projects_secret_column" or "unresolved_dependency_
        secret_exposure_risk"). Distinguishes "blocked for safety" from
        "blocked for generic incompleteness" in this state vocabulary --
        never applied to an already-COMPLETE profile (secret-column
        enforcement for a COMPLETE profile remains core.sqlservice.sql_
        validation's own unconditional BLOCK, unchanged by this state).
    PROFILE_STATE_LIMITED_COMPLETE: fresh for this build, parsed,
        INCOMPLETE, but proven eligible for the layered catalog fallback
        (Phase 7, Step 6E3B3, Part 3/4: profile.limited_complete_eligible
        is True -- catalog-confirmed output columns, known dependency/
        restriction/grain facts, no secret-exposure risk, a sufficient
        fraction of output columns individually resolved). STRICTLY
        narrower-trust than PROFILE_STATE_COMPLETE: a caller may use this
        state ONLY for a question compatible with the view's own
        suitability facts (see ViewSuitability -- entity counts still
        require a proven distinct key, aggregation still requires a
        compatible proven grain, an unrestricted "all" claim is still
        rejected when population/filter evidence is unknown); it is never
        a blanket "safe for anything" verdict the way COMPLETE is.
    PROFILE_STATE_LINEAGE_INCOMPLETE: fresh for this build, parsed, but one
        or more output columns/lineage facts could not be resolved
        (COMPLETION_INCOMPLETE), and NOT eligible for the LIMITED_COMPLETE
        fallback above.
    PROFILE_STATE_COMPLETE: fresh, fully resolved, current secret policy --
        the only state a caller may treat as sufficient evidence to let
        generated SQL reference the view for ANY compatible question
        shape, unconditionally (Step 6E3B2, Part 1 -- every other state
        blocks execution regardless of query shape, not merely
        unrestricted counts/aggregates, EXCEPT where a caller has been
        explicitly updated to also accept PROFILE_STATE_LIMITED_COMPLETE
        under its own narrower restrictions -- see core.sqlservice.sql_
        validation._validate_view_suitability's own enforcement of both)."""
    if profile is None:
        return PROFILE_STATE_MISSING
    if profile.schema_fingerprint != schema_fingerprint or profile.profiling_version != profiling_version:
        return PROFILE_STATE_STALE
    if current_definition_hash is not None and profile.definition_hash != current_definition_hash:
        return PROFILE_STATE_STALE
    if current_secret_policy_version is not None and profile.secret_policy_version != current_secret_policy_version:
        return PROFILE_STATE_STALE
    if profile.completion_status == COMPLETION_FAILED:
        return PROFILE_STATE_TYPED_FAILURE
    if profile.completion_status == COMPLETION_INCOMPLETE:
        if profile.limited_complete_eligible:
            return PROFILE_STATE_LIMITED_COMPLETE
        if (
            "own_output_projects_secret_column" in profile.limited_complete_reasons
            or "unresolved_dependency_secret_exposure_risk" in profile.limited_complete_reasons
        ):
            return PROFILE_STATE_UNSAFE
        return PROFILE_STATE_LINEAGE_INCOMPLETE
    return PROFILE_STATE_COMPLETE
