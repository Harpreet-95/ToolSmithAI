"""
Deterministic automatic generation of trusted SQL few-shot examples — SQL
rebuild Phase 5, Step 11A, corrected by Step 11A1, made bounded/streaming
by Step 11A2.

A generic, source-independent generator that mechanically builds safe
question/SQL example CANDIDATES from already-loaded structured schema
metadata (core.sqlservice.schema_context_retrieval.ContextDocument-shaped
retrieval documents) and the SAME authoritative confirmed-relationship
graph core.sqlservice.relationship_graph already builds for context
packing — nothing else. Every candidate is then run through the REAL,
unweakened trust pipeline this codebase already has: a synthetic but
genuine SchemaContextResult/SqlGenerationResult is constructed, handed to
core.sqlservice.sql_validation.validate_sql_candidate() for real
structural/read-only/table/column validation, and — only on a real
STATUS_OK result — handed to core.sqlservice.sql_example_candidate.
build_trusted_example_candidate() for real trust classification. This
module never bypasses, mocks, weakens, or duplicates either gate; it only
supplies them with mechanically-constructed, schema-derived inputs.

Step 11A1 corrected two defects a real dry run against a large production database
schema exposed: pattern starvation (single-table patterns alone could
exhaust the whole output budget before a single relationship pattern was
even attempted) and canonical quoted-identifier handling (a real table
name containing literal '[' / ']' characters was mis-rendered into SQL
text). See core.sqlservice.sql_validation.render_tsql_identifier's own
docstring for the identifier fix, still reused unchanged here.

Step 11A2 makes discovery itself BOUNDED AND STREAMING, so a larger or
more densely-connected schema than the reference database cannot make one generation call
consume unbounded memory or time:

  - Step 11A1 discovered and stored FOUR COMPLETE candidate pools (every
    structurally-possible attempt, fully validated) before ever selecting
    the final 200 — correct, but O(total structurally-possible attempts)
    in both memory and real-validation work, unbounded by anything the
    caller controls.
  - Step 11A2 replaces this with four independent, lazy, per-family
    iterators (_single_table_family_stream / _two_table_join_attempts /
    _three_table_relationship_path_attempts /
    _aggregate_over_relationship_attempts), each yielding raw attempts one
    at a time with no materialized pool at all, and a streaming, family-
    balanced round-robin selector (_stream_select_balanced) that pulls
    from them ONE CANDIDATE AT A TIME, in the exact same deterministic
    single-table/two-table-join/three-table-path/joined-aggregation
    rotation Step 11A1 already established, stopping the moment
    max_total_examples is reached OR every family's stream is exhausted —
    whichever comes first. A NEW hard ceiling,
    SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS, additionally bounds the
    total number of attempts (accepted OR rejected for any reason) one
    call will ever make, so a pathological schema (e.g. a relationship
    graph that manufactures an enormous number of duplicate-question
    attempts before ever producing 200 accepted ones) cannot make one
    generation call run arbitrarily long either — see
    SqlExampleGenerationResult.attempt_limit_reached.

Memory bound (Step 11A2, requirement 5):

    Memory = O(max_output_examples + max_candidate_attempts + schema_index)

This is deliberately NOT claimed to be constant. `schema_index` is
_eligible_objects()'s own output plus the confirmed-relationship graph
(core.sqlservice.relationship_graph.build_relationship_graph) — both scale
with the size of the CALLER-SUPPLIED `documents` sequence itself, which
this module never bounds (it is handed whatever the caller already loaded;
bounding schema-document volume is retrieval's own job, not generation's).
`max_output_examples` bounds the accepted-candidate list AND the per-object
count map (at most 3 objects touched per accepted candidate). `max_
candidate_attempts` bounds the three DEDUPLICATION key sets (seen_
questions/seen_sql/seen_hashes — each grows by at most one entry per
attempt) and the generation-attempt counter itself. Warnings are
INDEPENDENTLY, defensively bounded (_MAX_RETAINED_WARNINGS) so a
pathological stream of rejections cannot grow the warnings list without
bound even within one otherwise-legitimate attempt budget. At no point
does this module retain a complete pool of "every candidate discovered" —
only whatever is still live on the Python call stack of a suspended
generator (bounded local state: loop position and a handful of local
variables, never a materialized list of remaining work).

Scope — pure, in-memory, ZERO I/O of any kind:
  - No database connection (SQLite or live SQL Server) — accepts an
    already-loaded document sequence, never loads one itself.
  - No network/OpenAI call — schema/generation results are hand-
    constructed here, never a real embedding or text-to-SQL provider call.
  - No filesystem access.
  - Never inserts into sql_examples (core.sqlservice.sql_example_store is
    never imported here) and never computes an embedding.

Inputs are limited to exactly what requirement 2 of the Step 11A brief
names: the caller-supplied retrieval documents (canonical object/column
metadata, physical primary-key metadata, structured business_name/
synonyms already present in that metadata) and the CONFIRMED relationship
graph derived from those same documents' own
confirmed_relationship_edges/targets/sources metadata (never
inferred_relationship_*, which this module never reads at all — see
_eligible_objects and _confirmed_join_columns below). No golden-case
question/SQL/object/wording, no prior user question, no query history, no
result row, no manually-approved example, and no database-specific (or any
other source-specific) table name or business rule appears anywhere in
this module — it is generic over whatever documents it is handed. The
family-balanced streaming selection policy (_FAMILY_ORDER, _stream_select_
balanced) is likewise a fixed, generic, non-tunable round-robin — it has
no per-table, per-domain, or per-golden-case weighting of any kind.

Provenance is always PROVENANCE_SCHEMA_GENERATED_TEMPLATE; trust comes
from nowhere but validate_sql_candidate()'s own real structural-validation
gate (classify_trust_level's documented policy: this provenance is
trusted iff structural_validation_passed, which build_trusted_example_
candidate derives ONLY from a real, OK SqlValidationResult).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterator, Mapping, Sequence

from core.config import (
    SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS,
    SQL_EXAMPLE_GENERATION_MAX_EXAMPLES_PER_OBJECT,
    SQL_EXAMPLE_GENERATION_MAX_RELATIONSHIP_PATH_LENGTH,
    SQL_EXAMPLE_GENERATION_MAX_TOTAL_EXAMPLES,
)
from core.sqlservice.relationship_graph import (
    MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH,
    RelationshipGraph,
    build_relationship_graph,
)
from core.sqlservice.schema_context_retrieval import (
    STATUS_OK as _SCHEMA_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.sql_example_candidate import (
    ExampleCandidateRejected,
    TrustedSqlExampleCandidate,
    build_trusted_example_candidate,
)
from core.sqlservice.sql_example_model import (
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
    RESULT_SHAPE_COUNT,
    RESULT_SHAPE_GROUPED_AGGREGATION,
    RESULT_SHAPE_LIST,
    RESULT_SHAPE_TREND,
)
from core.sqlservice.sql_generation_service import (
    STATUS_OK as _GENERATION_STATUS_OK,
    SqlGenerationCandidate,
    SqlGenerationResult,
)
from core.sqlservice.sql_validation import (
    STATUS_OK as _VALIDATION_STATUS_OK,
    render_tsql_identifier,
    validate_sql_candidate,
)

__all__ = [
    "PATTERN_SINGLE_TABLE_ROW_COUNT",
    "PATTERN_SINGLE_TABLE_FILTERED_COUNT",
    "PATTERN_SINGLE_TABLE_GROUPED_AGGREGATION",
    "PATTERN_SINGLE_TABLE_ORDERED_LIST",
    "PATTERN_TWO_TABLE_JOIN",
    "PATTERN_THREE_TABLE_RELATIONSHIP_PATH",
    "PATTERN_AGGREGATE_OVER_RELATIONSHIP",
    "PATTERN_DATE_TREND",
    "ALL_PATTERNS",
    "ALLOWED_GENERATION_TIERS",
    "FAMILY_SINGLE_TABLE",
    "FAMILY_TWO_TABLE_JOIN",
    "FAMILY_THREE_TABLE_PATH",
    "FAMILY_AGGREGATE_OVER_RELATIONSHIP",
    "ALL_FAMILIES",
    "REJECTION_REASON_DUPLICATE_QUESTION",
    "REJECTION_REASON_DUPLICATE_SQL",
    "REJECTION_REASON_DUPLICATE_CONTENT_HASH",
    "REJECTION_REASON_PER_OBJECT_CAP",
    "REJECTION_REASON_VALIDATION_REJECTED",
    "REJECTION_REASON_TRUST_GATE_REJECTED",
    "ALL_REJECTION_REASONS",
    "GeneratedSqlExample",
    "SqlExampleGenerationResult",
    "generate_trusted_examples",
]

# ── Patterns (requirement 4) ────────────────────────────────────────────────

PATTERN_SINGLE_TABLE_ROW_COUNT = "SINGLE_TABLE_ROW_COUNT"
PATTERN_SINGLE_TABLE_FILTERED_COUNT = "SINGLE_TABLE_FILTERED_COUNT"
PATTERN_SINGLE_TABLE_GROUPED_AGGREGATION = "SINGLE_TABLE_GROUPED_AGGREGATION"
PATTERN_SINGLE_TABLE_ORDERED_LIST = "SINGLE_TABLE_ORDERED_LIST"
PATTERN_TWO_TABLE_JOIN = "TWO_TABLE_JOIN"
PATTERN_THREE_TABLE_RELATIONSHIP_PATH = "THREE_TABLE_RELATIONSHIP_PATH"
PATTERN_AGGREGATE_OVER_RELATIONSHIP = "AGGREGATE_OVER_RELATIONSHIP"
PATTERN_DATE_TREND = "DATE_TREND"

ALL_PATTERNS = frozenset({
    PATTERN_SINGLE_TABLE_ROW_COUNT, PATTERN_SINGLE_TABLE_FILTERED_COUNT,
    PATTERN_SINGLE_TABLE_GROUPED_AGGREGATION, PATTERN_SINGLE_TABLE_ORDERED_LIST,
    PATTERN_TWO_TABLE_JOIN, PATTERN_THREE_TABLE_RELATIONSHIP_PATH,
    PATTERN_AGGREGATE_OVER_RELATIONSHIP, PATTERN_DATE_TREND,
})

# Requirement 9 — "tier policy excludes Tier 3/4": a fixed policy, never a
# caller-tunable parameter (an object outside this set is never a
# candidate anchor/join target, full stop, regardless of any other
# configuration this module accepts).
ALLOWED_GENERATION_TIERS = frozenset({1, 2})

_TOP_N_LIST_ROWS = 20
_TSQL_DIALECT_FINGERPRINT_PLACEHOLDER_PROVIDER = "generated"  # embedding_provider/model placeholders for the synthetic SchemaContextResult -- never a real provider, never called

# Step 11A2, requirement 5 — an independent, defensive bound on how many
# warning STRINGS one generate_trusted_examples() call ever retains,
# regardless of max_candidate_attempts. A pathological stream of
# duplicate/rejected attempts (bounded by max_candidate_attempts, but that
# ceiling can legitimately be configured very large) must never turn
# `warnings` itself into an unbounded list. A fixed, generic constant —
# never schema-size-dependent, never per-table-tunable.
_MAX_RETAINED_WARNINGS = 500


# ── Pattern families (Step 11A1) — the four peer allocation units
#    requirement 2 names explicitly. Every PATTERN_* belongs to exactly one
#    family; the four single-table sub-patterns are deliberately ONE family
#    (they all describe "a question about one table alone"), never split
#    further, so a schema rich in single-table patterns cannot itself
#    starve the family-level allocation the way un-grouped pattern-level
#    round-robin would. Fixed, generic order — never data-dependent, never
#    tuned per table/domain/golden-case (requirement 5): matches the exact
#    order requirement 2/6 themselves list these families in. ─────────────

FAMILY_SINGLE_TABLE = "SINGLE_TABLE"
FAMILY_TWO_TABLE_JOIN = "TWO_TABLE_JOIN"
FAMILY_THREE_TABLE_PATH = "THREE_TABLE_PATH"
FAMILY_AGGREGATE_OVER_RELATIONSHIP = "AGGREGATE_OVER_RELATIONSHIP"

ALL_FAMILIES = frozenset({
    FAMILY_SINGLE_TABLE, FAMILY_TWO_TABLE_JOIN, FAMILY_THREE_TABLE_PATH, FAMILY_AGGREGATE_OVER_RELATIONSHIP,
})

_FAMILY_ORDER: tuple[str, ...] = (
    FAMILY_SINGLE_TABLE, FAMILY_TWO_TABLE_JOIN, FAMILY_THREE_TABLE_PATH, FAMILY_AGGREGATE_OVER_RELATIONSHIP,
)

# ── Rejection reasons (Step 11A2, requirement 7) — the fixed, exhaustive
#    vocabulary of every way one candidate ATTEMPT can fail to be accepted.
#    Every one of these increments the SAME shared attempt counter
#    (requirement 3) — the invariant candidate_attempt_count ==
#    generated_count + sum(rejected_counts_by_reason.values()) always
#    holds. ─────────────────────────────────────────────────────────────

REJECTION_REASON_DUPLICATE_QUESTION = "DUPLICATE_QUESTION"
REJECTION_REASON_DUPLICATE_SQL = "DUPLICATE_SQL"
REJECTION_REASON_DUPLICATE_CONTENT_HASH = "DUPLICATE_CONTENT_HASH"
REJECTION_REASON_PER_OBJECT_CAP = "PER_OBJECT_CAP"
REJECTION_REASON_VALIDATION_REJECTED = "VALIDATION_REJECTED"
REJECTION_REASON_TRUST_GATE_REJECTED = "TRUST_GATE_REJECTED"

ALL_REJECTION_REASONS = frozenset({
    REJECTION_REASON_DUPLICATE_QUESTION, REJECTION_REASON_DUPLICATE_SQL, REJECTION_REASON_DUPLICATE_CONTENT_HASH,
    REJECTION_REASON_PER_OBJECT_CAP, REJECTION_REASON_VALIDATION_REJECTED, REJECTION_REASON_TRUST_GATE_REJECTED,
})


# ── Public data model (requirement 12) ─────────────────────────────────────


@dataclass(frozen=True)
class GeneratedSqlExample:
    """One successfully generated, ALREADY gate-validated trusted example.
    `candidate` is a real core.sqlservice.sql_example_candidate.
    TrustedSqlExampleCandidate (that class cannot be hand-constructed —
    see its own module), ready to hand to
    core.sqlservice.sql_example_store.insert_trusted_example() unchanged.
    `pattern`/`anchor_object_fqns` are observability only."""

    pattern: str
    anchor_object_fqns: tuple[str, ...]
    candidate: TrustedSqlExampleCandidate


@dataclass(frozen=True)
class SqlExampleGenerationResult:
    """
    Result of one generate_trusted_examples() call. Never mutable — every
    collection field is a tuple or a MappingProxyType.

    generated_count: len(candidates) — how many examples survived every
        gate (uniqueness, real validate_sql_candidate, real
        build_trusted_example_candidate, and the per-object/global-cap
        streaming selection).
    skipped_count: total rejected attempts, for ANY reason — always equal
        to sum(rejected_counts_by_reason.values()). Kept (rather than
        removed) for continuity with every prior step's own field of this
        name and meaning.
    counts_by_pattern: ACCEPTED (final) count per PATTERN_* key — only
        patterns that produced at least one example appear.
    candidate_attempt_count: Step 11A2 addition. Total candidate attempts
        actually made — every accepted candidate PLUS every rejection of
        any kind (duplicate question/SQL/content-hash, per-object cap,
        validation rejection, trust-gate rejection). Invariant:
        candidate_attempt_count == generated_count +
        sum(rejected_counts_by_reason.values()). Never counts a
        structural, pre-attempt exclusion (an ineligible Tier 3/4 object,
        an object missing trustworthy column metadata, or a confirmed
        relationship edge whose declared join column isn't in a
        trustworthy catalog) — those are reported only as `warnings`, the
        same distinction Step 11A1 already drew.
    attempt_limit: the max_candidate_attempts value actually used for this
        call (echoes the caller's own value, or the configured default).
    attempt_limit_reached: True iff candidate_attempt_count reached
        attempt_limit before every family's stream was naturally exhausted
        and before max_total_examples was reached — i.e. this run stopped
        EARLY and may not have found every candidate a larger attempt
        budget would have. False whenever the run stopped because the
        output cap was reached or every family's stream ran dry on its
        own. Always reported, never silent (requirement 3).
    rejected_counts_by_reason: total rejected-attempt count per
        REJECTION_REASON_* key — only reasons that occurred at least once
        appear.
    exhausted_families: FAMILY_* names (sorted, deterministic) whose
        stream was pulled all the way to natural exhaustion (ran out of
        structurally-possible attempts on its own) during this call —
        never includes a family that still had more to give when the
        output cap or the attempt limit stopped the whole run first.
    warnings: every warning recorded, defensively bounded at
        _MAX_RETAINED_WARNINGS entries (a final, single truncation notice
        is appended in place of anything past that bound — the list
        itself never grows without bound even against a pathological
        stream of rejections).
    """

    source_id: int
    build_id: int
    schema_fingerprint: str
    candidates: tuple[GeneratedSqlExample, ...]
    generated_count: int
    skipped_count: int
    counts_by_pattern: Mapping[str, int]
    candidate_attempt_count: int
    attempt_limit: int
    attempt_limit_reached: bool
    rejected_counts_by_reason: Mapping[str, int]
    exhausted_families: tuple[str, ...]
    warnings: tuple[str, ...]


# ── Text humanization (pure string formatting -- NEVER SQL structural
#    authority; regex is fine here, the Step 9B/10 "no regex" rule is
#    specifically about SQL AST/column decisions, not English word
#    splitting) ────────────────────────────────────────────────────────────

_WORD_BOUNDARY_RE = re.compile(r"[_\-]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _humanize(identifier: str) -> str:
    """Deterministic PascalCase/snake_case/SCREAMING -> lowercase words,
    e.g. 'ClassSignups' -> 'class signups', 'student_id' -> 'student id'.
    Falls back to the lowercased identifier verbatim if it contains no
    recognizable word boundary at all."""
    words = [w for w in _WORD_BOUNDARY_RE.split(identifier) if w]
    return " ".join(w.lower() for w in words) if words else identifier.lower()


def _pluralize(words: str) -> str:
    return words if words.endswith("s") else f"{words}s"


def _display_name(object_fqn: str, metadata: Mapping[str, object]) -> str:
    """Requirement 6: prefer a structured business_name, then the first
    (sorted) structured synonym, then a safely humanized bare object name
    -- NEVER a fabricated or inferred business label."""
    business_name = metadata.get("business_name")
    if isinstance(business_name, str) and business_name.strip():
        return business_name.strip()
    synonyms = metadata.get("synonyms") or ()
    usable_synonyms = sorted(s.strip() for s in synonyms if isinstance(s, str) and s.strip())
    if usable_synonyms:
        return usable_synonyms[0]
    _, _, bare_name = object_fqn.rpartition(".")
    return _humanize(bare_name)


def _normalize_for_dedup(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


# ── Object eligibility (requirements 2/4/9) ─────────────────────────────────


@dataclass(frozen=True)
class _ObjectInfo:
    object_fqn: str
    object_type: str
    document: ContextDocument
    columns_lower_to_canonical: Mapping[str, str]
    primary_key_columns: tuple[str, ...]
    status_columns: tuple[str, ...]
    date_columns: tuple[str, ...]


def _has_trustworthy_columns(metadata: Mapping[str, object]) -> bool:
    raw = metadata.get("column_names")
    return (
        isinstance(raw, (list, tuple)) and bool(raw)
        and all(isinstance(c, str) and c.strip() for c in raw)
    )


def _canonicalize_declared_columns(
    declared: Sequence[str], columns_lower_to_canonical: Mapping[str, str],
) -> tuple[str, ...] | None:
    """Every entry in `declared` (e.g. metadata's own primary_key_columns/
    status_columns/date_columns) must actually exist in this SAME
    object's own trustworthy column catalog -- returns None (never a
    partial list) if even one does not, so a caller can treat the whole
    declared set as untrustworthy rather than silently dropping entries."""
    resolved = []
    for name in declared:
        canonical = columns_lower_to_canonical.get(str(name).lower())
        if canonical is None:
            return None
        resolved.append(canonical)
    return tuple(resolved)


def _eligible_objects(documents: Sequence[ContextDocument]) -> tuple[tuple[_ObjectInfo, ...], tuple[str, ...]]:
    """
    Requirement 9's "skip objects missing trustworthy column metadata" and
    "tier policy excludes Tier 3/4" — both enforced here, once, before any
    pattern is even attempted. Returns (eligible objects sorted
    deterministically by object_fqn, warnings).
    """
    infos: list[_ObjectInfo] = []
    warnings: list[str] = []
    for document in documents:
        if document.retrieval_tier not in ALLOWED_GENERATION_TIERS:
            continue
        metadata = document.metadata
        if not _has_trustworthy_columns(metadata):
            warnings.append(
                f"skipped object {document.object_fqn!r}: no trustworthy structured column_names metadata"
            )
            continue

        columns_lower_to_canonical = {str(c).lower(): str(c) for c in metadata.get("column_names", ())}

        pk = _canonicalize_declared_columns(metadata.get("primary_key_columns") or (), columns_lower_to_canonical)
        if pk is None:
            warnings.append(
                f"object {document.object_fqn!r}: primary_key_columns metadata references an unknown "
                f"column -- treated as having no usable primary key"
            )
            pk = ()

        status_cols = _canonicalize_declared_columns(metadata.get("status_columns") or (), columns_lower_to_canonical)
        if status_cols is None:
            status_cols = ()
        date_cols = _canonicalize_declared_columns(metadata.get("date_columns") or (), columns_lower_to_canonical)
        if date_cols is None:
            date_cols = ()

        infos.append(_ObjectInfo(
            object_fqn=document.object_fqn, object_type=document.object_type, document=document,
            columns_lower_to_canonical=MappingProxyType(columns_lower_to_canonical),
            primary_key_columns=pk, status_columns=status_cols, date_columns=date_cols,
        ))

    infos.sort(key=lambda info: info.object_fqn.lower())
    return tuple(infos), tuple(warnings)


def _projection_columns(info: _ObjectInfo, limit: int = 3) -> tuple[str, ...]:
    """A small, deterministic, always-available set of columns to project
    for a list-shaped pattern: the primary key if known, else the first
    (sorted) columns from the trustworthy catalog -- never invented, never
    the SELECT * this module always avoids."""
    if info.primary_key_columns:
        return info.primary_key_columns[:limit]
    return tuple(sorted(info.columns_lower_to_canonical.values()))[:limit]


def _order_column(info: _ObjectInfo) -> str:
    if info.date_columns:
        return info.date_columns[0]
    if info.primary_key_columns:
        return info.primary_key_columns[0]
    return sorted(info.columns_lower_to_canonical.values())[0]


# ── Confirmed join-column resolution (requirement: authoritative
#    relationships only; "missing join-column rejection"; composite-key
#    handling) ────────────────────────────────────────────────────────────


def _parse_other_descriptor(other: object) -> tuple[str, str] | None:
    """'schema.table.column' -> ('schema.table', 'column'); None if the
    descriptor is not shaped as expected -- never guessed at."""
    text = str(other)
    object_fqn, sep, column = text.rpartition(".")
    if not sep or not object_fqn or not column:
        return None
    return object_fqn, column


def _confirmed_join_columns(
    obj_by_lower: Mapping[str, _ObjectInfo], fqn_a: str, fqn_b: str,
) -> tuple[tuple[str, str], ...]:
    """
    Every CONFIRMED (canonical_column_on_a, canonical_column_on_b) pair
    directly connecting fqn_a and fqn_b — reads ONLY
    metadata["confirmed_relationship_edges"] (never "inferred_*", which
    this function never even looks at) from EITHER side (a physical FK is
    typically recorded outbound on the child and inbound on the parent;
    checking both sides and deduplicating means a caller never has to
    know which side originally declared it). More than one pair is a
    composite-key relationship — every pair is ANDed together by the
    caller building the ON clause.

    A pair is only ever returned when BOTH sides' declared column
    actually exists in that side's OWN trustworthy column catalog
    (requirement: "missing join-column rejection") — an edge whose
    descriptor is malformed, or whose column isn't in the catalog, is
    silently excluded from the returned set rather than guessed at or
    coerced. Deterministically ordered.
    """
    a_lower, b_lower = fqn_a.lower(), fqn_b.lower()
    info_a, info_b = obj_by_lower.get(a_lower), obj_by_lower.get(b_lower)
    if info_a is None or info_b is None:
        return ()

    pairs: set[tuple[str, str]] = set()

    for edge in info_a.document.metadata.get("confirmed_relationship_edges") or ():
        parsed = _parse_other_descriptor(edge.get("other", "")) if isinstance(edge, Mapping) else None
        if parsed is None or parsed[0].lower() != b_lower:
            continue
        col_a = info_a.columns_lower_to_canonical.get(str(edge.get("column", "")).lower())
        col_b = info_b.columns_lower_to_canonical.get(parsed[1].lower())
        if col_a and col_b:
            pairs.add((col_a, col_b))

    for edge in info_b.document.metadata.get("confirmed_relationship_edges") or ():
        parsed = _parse_other_descriptor(edge.get("other", "")) if isinstance(edge, Mapping) else None
        if parsed is None or parsed[0].lower() != a_lower:
            continue
        col_b = info_b.columns_lower_to_canonical.get(str(edge.get("column", "")).lower())
        col_a = info_a.columns_lower_to_canonical.get(parsed[1].lower())
        if col_a and col_b:
            pairs.add((col_a, col_b))

    return tuple(sorted(pairs))


# ── T-SQL-safe identifier rendering (Step 11A1, Problem 2) ─────────────────
#
# render_tsql_identifier (core.sqlservice.sql_validation) is the ONLY
# authority this module ever uses to turn a canonical schema/table/column
# name into SQL TEXT — reused, never duplicated as a second regex-only
# resolver. The canonical identity used for referenced_object_fqns/
# anchor_object_fqns/join_path/dedup/output NEVER goes through this
# rendering — only the literal SQL string embeds the rendered form.


def _render_object_reference(object_fqn: str) -> str:
    """"schema.table" (the canonical, possibly-needs-quoting raw identity)
    -> safely-rendered "schema.table" SQL text. Splits on the FIRST '.'
    only — this project's own established convention (see
    core.sqlservice.schema_context_retrieval's own docstring) is that a
    schema or table name never contains a literal dot itself, only
    (rarely) other special characters like brackets, which render_tsql_
    identifier already handles."""
    schema, _sep, obj = object_fqn.partition(".")
    return f"{render_tsql_identifier(schema)}.{render_tsql_identifier(obj)}"


def _qualified_column(alias: str, column_name: str) -> str:
    """"alias.column" SQL text, both parts safely rendered. `alias` is
    always one of this module's own literal, always-safe "t1"/"t2"/"t3"
    tokens in practice, but is still routed through the SAME safety
    rendering as every other identifier here — never a special case."""
    return f"{render_tsql_identifier(alias)}.{render_tsql_identifier(column_name)}"


def _on_clause(alias_a: str, alias_b: str, join_columns: Sequence[tuple[str, str]]) -> str:
    return " AND ".join(
        f"{_qualified_column(alias_a, col_a)} = {_qualified_column(alias_b, col_b)}"
        for col_a, col_b in join_columns
    )


# ── Synthetic pipeline plumbing (requirement 7 — the REAL gates, never
#    bypassed/mocked/duplicated) ────────────────────────────────────────────


def _build_schema_context(
    involved: Sequence[_ObjectInfo], *, build_id: int, schema_fingerprint: str,
) -> SchemaContextResult:
    documents = tuple(info.document for info in involved)
    context_text = "\n\n".join(d.document_text for d in documents)
    return SchemaContextResult(
        status=_SCHEMA_STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider=_TSQL_DIALECT_FINGERPRINT_PLACEHOLDER_PROVIDER,
        embedding_model=_TSQL_DIALECT_FINGERPRINT_PLACEHOLDER_PROVIDER, embedding_dimensions=1,
        documents=documents, context_text=context_text, total_characters=len(context_text),
        truncated=False, warnings=(), error=None,
    )


def _build_generation_result(
    sql: str, referenced_object_fqns: Sequence[str], *, build_id: int, schema_fingerprint: str,
) -> SqlGenerationResult:
    candidate = SqlGenerationCandidate(
        sql=sql, interpretation="Deterministically generated from schema metadata.",
        referenced_object_fqns=tuple(referenced_object_fqns), assumptions=(),
        needs_clarification=False, clarification_question=None,
    )
    return SqlGenerationResult(
        status=_GENERATION_STATUS_OK, candidate=candidate, build_id=build_id,
        schema_fingerprint=schema_fingerprint, provider_id=_TSQL_DIALECT_FINGERPRINT_PLACEHOLDER_PROVIDER,
        model=_TSQL_DIALECT_FINGERPRINT_PLACEHOLDER_PROVIDER, warnings=(), error=None,
    )


# ── Candidate attempts — pure, lazy DISCOVERY output, not yet validated/
#    gated/capped. One _CandidateAttempt per structurally-possible pattern
#    instance; _GenerationState.try_validate() below is the ONLY place any
#    of these ever reaches the real validation/trust gates. ───────────────


@dataclass(frozen=True)
class _CandidateAttempt:
    pattern: str
    family: str
    sql: str
    referenced_object_fqns: tuple[str, ...]
    question: str
    result_shape: str
    join_path: tuple[str, ...]
    anchor_object_fqns: tuple[str, ...]
    involved: tuple[_ObjectInfo, ...]


# ── Discovery/validation state (Step 11A2: owns the SHARED, bounded
#    concerns every per-family stream draws from — deduplication, the
#    attempt counter/limit, warnings, and rejection-reason bookkeeping.
#    Never owns the per-object cap or family-exhaustion tracking, which
#    are the streaming selector's own, separate responsibility — see
#    _stream_select_balanced.) ───────────────────────────────────────────


class _GenerationState:
    """Mutable ONLY inside one generate_trusted_examples() call — never
    exposed. Bounded memory (requirement 5): seen_questions/seen_sql/
    seen_hashes each grow by at most one entry per attempt, and
    attempt_count is hard-capped at max_candidate_attempts, so all three
    sets are O(max_candidate_attempts); warnings is independently capped
    at _MAX_RETAINED_WARNINGS regardless of attempt volume."""

    def __init__(self, *, source_id: int, build_id: int, schema_fingerprint: str, max_candidate_attempts: int) -> None:
        self.source_id = source_id
        self.build_id = build_id
        self.schema_fingerprint = schema_fingerprint
        self.max_candidate_attempts = max_candidate_attempts
        self.attempt_count = 0
        self.warnings: list[str] = []
        self.rejected_counts_by_reason: Counter = Counter()
        self._warnings_truncated = False
        self.seen_questions: set[str] = set()
        self.seen_sql: set[str] = set()
        self.seen_hashes: set[str] = set()
        self._per_object_cap_warned: set[str] = set()
        self._warned_missing_join_keys: set[tuple] = set()

    @property
    def attempt_limit_reached(self) -> bool:
        return self.attempt_count >= self.max_candidate_attempts

    def _add_warning(self, message: str) -> None:
        if len(self.warnings) >= _MAX_RETAINED_WARNINGS:
            if not self._warnings_truncated:
                self._warnings_truncated = True
                self.warnings.append(
                    f"warning output truncated after {_MAX_RETAINED_WARNINGS} entries -- "
                    f"further warnings are suppressed for this run, never silently dropped work"
                )
            return
        self.warnings.append(message)

    def warn_missing_join_columns_once(self, key: tuple, message: str) -> None:
        """A structural fact about the confirmed-relationship graph itself
        (an edge exists but no side's declared join column is in a
        trustworthy catalog) — never a candidate ATTEMPT (no SQL was ever
        built), so this never touches attempt_count/rejected_counts_by_
        reason, exactly like Step 11A1's own behavior. Deduped by `key`
        because Step 11A2's THREE independent relationship iterators
        (two-table join, aggregate-over-relationship, three-table path)
        each independently discover the SAME missing-anchor-to-neighbor
        edge on their own traversal — without this, the identical warning
        could be recorded up to three times for one edge."""
        if key in self._warned_missing_join_keys:
            return
        self._warned_missing_join_keys.add(key)
        self._add_warning(message)

    def try_validate(self, attempt: _CandidateAttempt) -> GeneratedSqlExample | None:
        """Consumes exactly ONE unit of the attempt budget for `attempt`
        and returns a validated GeneratedSqlExample, or None if rejected
        for a duplicate/validation/trust reason. The per-object cap is
        NEVER checked here — see _stream_select_balanced, which is the
        only place with the live, cross-family per-object counts needed to
        check it; a candidate this function accepts may still be rejected
        by the caller for that reason (see REJECTION_REASON_PER_OBJECT_CAP
        and _GenerationState.reject_for_object_cap, which does NOT
        increment attempt_count again — this call already did)."""
        self.attempt_count += 1

        normalized_question = _normalize_for_dedup(attempt.question)
        normalized_sql = _normalize_for_dedup(attempt.sql)
        if normalized_question in self.seen_questions:
            self.rejected_counts_by_reason[REJECTION_REASON_DUPLICATE_QUESTION] += 1
            self._add_warning(
                f"skipped duplicate normalized question for pattern {attempt.pattern}: {attempt.question!r}"
            )
            return None
        if normalized_sql in self.seen_sql:
            self.rejected_counts_by_reason[REJECTION_REASON_DUPLICATE_SQL] += 1
            self._add_warning(f"skipped duplicate normalized SQL for pattern {attempt.pattern}")
            return None

        schema_context = _build_schema_context(
            attempt.involved, build_id=self.build_id, schema_fingerprint=self.schema_fingerprint,
        )
        generation_result = _build_generation_result(
            attempt.sql, attempt.referenced_object_fqns, build_id=self.build_id, schema_fingerprint=self.schema_fingerprint,
        )
        validation_result = validate_sql_candidate(generation_result, schema_context)
        if validation_result.status != _VALIDATION_STATUS_OK:
            self.rejected_counts_by_reason[REJECTION_REASON_VALIDATION_REJECTED] += 1
            self._add_warning(
                f"skipped {attempt.pattern} candidate for {attempt.anchor_object_fqns!r}: validation rejected it "
                f"({validation_result.error})"
            )
            return None

        try:
            trusted_candidate = build_trusted_example_candidate(
                source_id=self.source_id, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
                validation_result=validation_result, question_pattern=attempt.question, result_shape=attempt.result_shape,
                join_path=attempt.join_path,
            )
        except ExampleCandidateRejected as exc:
            # Defensive only -- should not occur given a real STATUS_OK
            # validation_result and PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            # which classify_trust_level trusts on structural validation
            # alone. Never silently swallowed: recorded and counted.
            self.rejected_counts_by_reason[REJECTION_REASON_TRUST_GATE_REJECTED] += 1
            self._add_warning(
                f"skipped {attempt.pattern} candidate for {attempt.anchor_object_fqns!r}: trust gate rejected it ({exc})"
            )
            return None

        if trusted_candidate.content_hash in self.seen_hashes:
            self.rejected_counts_by_reason[REJECTION_REASON_DUPLICATE_CONTENT_HASH] += 1
            self._add_warning(f"skipped duplicate content hash for pattern {attempt.pattern}")
            return None

        self.seen_questions.add(normalized_question)
        self.seen_sql.add(normalized_sql)
        self.seen_hashes.add(trusted_candidate.content_hash)
        return GeneratedSqlExample(
            pattern=attempt.pattern, anchor_object_fqns=attempt.anchor_object_fqns, candidate=trusted_candidate,
        )

    def reject_for_object_cap(self, candidate: GeneratedSqlExample, capped_on: Sequence[str], max_examples_per_object: int) -> None:
        """The candidate already consumed one attempt (via try_validate,
        which produced it) — this records ONLY the rejection reason, never
        a second attempt-count increment for the same underlying attempt."""
        self.rejected_counts_by_reason[REJECTION_REASON_PER_OBJECT_CAP] += 1
        for fqn in capped_on:
            key = fqn.lower()
            if key not in self._per_object_cap_warned:
                self._per_object_cap_warned.add(key)
                self._add_warning(
                    f"object {fqn!r} reached the per-object example cap "
                    f"({max_examples_per_object}) -- further candidates for it are skipped"
                )


# ── Per-pattern SQL/question construction (requirements 5/6) — pure,
#    LAZY discovery: each is a generator function yielding one
#    _CandidateAttempt at a time, never touching _GenerationState or any
#    cap, and never materializing a list of remaining work. ────────────────


def _single_table_attempts(info: _ObjectInfo) -> Iterator[_CandidateAttempt]:
    fqn = info.object_fqn
    rendered_fqn = _render_object_reference(fqn)
    display_plural = _pluralize(_display_name(fqn, info.document.metadata))

    # PATTERN_SINGLE_TABLE_ROW_COUNT
    yield _CandidateAttempt(
        pattern=PATTERN_SINGLE_TABLE_ROW_COUNT, family=FAMILY_SINGLE_TABLE,
        sql=f"SELECT COUNT(*) AS RowCount FROM {rendered_fqn} AS t1",
        referenced_object_fqns=(fqn,), question=f"How many {display_plural} are there?",
        result_shape=RESULT_SHAPE_COUNT, join_path=(), anchor_object_fqns=(fqn,), involved=(info,),
    )

    # PATTERN_SINGLE_TABLE_FILTERED_COUNT -- requirement 4: only when a
    # safe categorical/status/date column exists, and requirement 5:
    # never a fabricated literal -- IS NOT NULL invents no value at all.
    filter_column = (info.status_columns or info.date_columns or (None,))[0]
    if filter_column is not None:
        rendered_filter_column = _qualified_column("t1", filter_column)
        yield _CandidateAttempt(
            pattern=PATTERN_SINGLE_TABLE_FILTERED_COUNT, family=FAMILY_SINGLE_TABLE,
            sql=f"SELECT COUNT(*) AS RowCount FROM {rendered_fqn} AS t1 WHERE {rendered_filter_column} IS NOT NULL",
            referenced_object_fqns=(fqn,),
            question=f"How many {display_plural} have a recorded {_humanize(filter_column)}?",
            result_shape=RESULT_SHAPE_COUNT, join_path=(), anchor_object_fqns=(fqn,), involved=(info,),
        )

    # PATTERN_SINGLE_TABLE_GROUPED_AGGREGATION -- requires a status column.
    if info.status_columns:
        group_column = info.status_columns[0]
        rendered_group_column = _qualified_column("t1", group_column)
        yield _CandidateAttempt(
            pattern=PATTERN_SINGLE_TABLE_GROUPED_AGGREGATION, family=FAMILY_SINGLE_TABLE,
            sql=(
                f"SELECT {rendered_group_column}, COUNT(*) AS RowCount FROM {rendered_fqn} AS t1 "
                f"GROUP BY {rendered_group_column}"
            ),
            referenced_object_fqns=(fqn,),
            question=f"How many {display_plural} are there for each {_humanize(group_column)}?",
            result_shape=RESULT_SHAPE_GROUPED_AGGREGATION, join_path=(), anchor_object_fqns=(fqn,), involved=(info,),
        )

    # PATTERN_SINGLE_TABLE_ORDERED_LIST
    projection = _projection_columns(info)
    order_column = _order_column(info)
    projection_sql = ", ".join(_qualified_column("t1", col) for col in projection)
    yield _CandidateAttempt(
        pattern=PATTERN_SINGLE_TABLE_ORDERED_LIST, family=FAMILY_SINGLE_TABLE,
        sql=(
            f"SELECT TOP {_TOP_N_LIST_ROWS} {projection_sql} FROM {rendered_fqn} AS t1 "
            f"ORDER BY {_qualified_column('t1', order_column)}"
        ),
        referenced_object_fqns=(fqn,),
        question=f"List {display_plural}, ordered by {_humanize(order_column)}.",
        result_shape=RESULT_SHAPE_LIST, join_path=(), anchor_object_fqns=(fqn,), involved=(info,),
    )

    # PATTERN_DATE_TREND -- requires a real structured date column.
    if info.date_columns:
        date_column = info.date_columns[0]
        rendered_date_column = _qualified_column("t1", date_column)
        yield _CandidateAttempt(
            pattern=PATTERN_DATE_TREND, family=FAMILY_SINGLE_TABLE,
            sql=(
                f"SELECT {rendered_date_column}, COUNT(*) AS RowCount FROM {rendered_fqn} AS t1 "
                f"GROUP BY {rendered_date_column} ORDER BY {rendered_date_column}"
            ),
            referenced_object_fqns=(fqn,),
            question=f"How many {display_plural} are there by {_humanize(date_column)}?",
            result_shape=RESULT_SHAPE_TREND, join_path=(), anchor_object_fqns=(fqn,), involved=(info,),
        )


def _single_table_family_stream(eligible_objects: Sequence[_ObjectInfo]) -> Iterator[_CandidateAttempt]:
    """Requirement 1's single-table iterator: flattens
    _single_table_attempts() across every eligible object, lazily — the
    outer `for` suspends exactly at each inner `yield`, so no object past
    the one currently being considered is ever touched ahead of time."""
    for info in eligible_objects:
        yield from _single_table_attempts(info)


# ── Relationship attempt iterators (Step 11A2, requirement 1) — THREE
#    INDEPENDENT lazy generators, one per relationship family, each doing
#    its OWN anchor -> neighbor (-> second-hop neighbor) traversal. This
#    trades a little redundant (cheap, in-memory, no-I/O) graph-adjacency
#    re-walking for each iterator being genuinely, independently pollable
#    one candidate at a time — no shared internal buffering/multiplexing
#    state to reason about. `state` is threaded through ONLY for the
#    bounded, deduplicated missing-join-column warning (see
#    warn_missing_join_columns_once) — never for validation, which stays
#    the caller's own job via state.try_validate() on the yielded attempt.
#
#    Cycle/duplicate-path prevention (requirement 4): the anchor/neighbor
#    loops already structurally exclude going back to the anchor or
#    staying at the bridge object for the third-hop case
#    (`c_lower in (anchor_lower, b_lower)`), and
#    _three_table_relationship_path_attempts additionally tracks every
#    (anchor, bridge, target) triple it has already yielded in
#    `seen_path_keys` — bounded by how many triples this call actually
#    considers (itself bounded by the shared attempt budget, since a
#    caller only ever pulls as many items as it needs), so even a graph
#    with multiple parallel edges between the same two objects, or a
#    genuine cycle (A-B, B-C, C-A), can never yield the same logical path
#    twice or loop unboundedly — this is a single, fixed-depth (<= 2 hops)
#    traversal, never recursive. ─────────────────────────────────────────


def _two_table_join_attempts(
    state: _GenerationState, obj_by_lower: Mapping[str, _ObjectInfo], graph: RelationshipGraph,
    ordered_objects: Sequence[_ObjectInfo],
) -> Iterator[_CandidateAttempt]:
    for anchor in ordered_objects:
        anchor_lower = anchor.object_fqn.lower()
        for b_lower in sorted(graph.adjacency.get(anchor_lower, ())):
            info_b = obj_by_lower.get(b_lower)
            if info_b is None:
                continue
            join_ab = _confirmed_join_columns(obj_by_lower, anchor.object_fqn, info_b.object_fqn)
            if not join_ab:
                state.warn_missing_join_columns_once(
                    ("AB", anchor_lower, b_lower),
                    f"skipped relationship {anchor.object_fqn!r} -> {info_b.object_fqn!r}: "
                    f"no usable confirmed join column found",
                )
                continue

            display_a = _display_name(anchor.object_fqn, anchor.document.metadata)
            display_b_plural = _pluralize(_display_name(info_b.object_fqn, info_b.document.metadata))
            on_ab = _on_clause("t1", "t2", join_ab)
            join_path_ab = tuple(f"{anchor.object_fqn}.{c1} -> {info_b.object_fqn}.{c2}" for c1, c2 in join_ab)
            projection_a = _projection_columns(anchor, limit=2)
            projection_b = _projection_columns(info_b, limit=2)
            select_list = ", ".join(
                [_qualified_column("t1", c) for c in projection_a] + [_qualified_column("t2", c) for c in projection_b]
            )
            rendered_anchor = _render_object_reference(anchor.object_fqn)
            rendered_b = _render_object_reference(info_b.object_fqn)

            yield _CandidateAttempt(
                pattern=PATTERN_TWO_TABLE_JOIN, family=FAMILY_TWO_TABLE_JOIN,
                sql=(
                    f"SELECT TOP {_TOP_N_LIST_ROWS} {select_list} FROM {rendered_anchor} AS t1 "
                    f"JOIN {rendered_b} AS t2 ON {on_ab}"
                ),
                referenced_object_fqns=(anchor.object_fqn, info_b.object_fqn),
                question=f"For each {display_a}, show the related {display_b_plural}.",
                result_shape=RESULT_SHAPE_LIST, join_path=join_path_ab,
                anchor_object_fqns=(anchor.object_fqn, info_b.object_fqn), involved=(anchor, info_b),
            )


def _aggregate_over_relationship_attempts(
    state: _GenerationState, obj_by_lower: Mapping[str, _ObjectInfo], graph: RelationshipGraph,
    ordered_objects: Sequence[_ObjectInfo],
) -> Iterator[_CandidateAttempt]:
    for anchor in ordered_objects:
        anchor_lower = anchor.object_fqn.lower()
        for b_lower in sorted(graph.adjacency.get(anchor_lower, ())):
            info_b = obj_by_lower.get(b_lower)
            if info_b is None:
                continue
            join_ab = _confirmed_join_columns(obj_by_lower, anchor.object_fqn, info_b.object_fqn)
            if not join_ab:
                state.warn_missing_join_columns_once(
                    ("AB", anchor_lower, b_lower),
                    f"skipped relationship {anchor.object_fqn!r} -> {info_b.object_fqn!r}: "
                    f"no usable confirmed join column found",
                )
                continue

            # "how many B per A" only makes sense, and is only attempted,
            # when the anchor is genuinely the referenced ("one") side of
            # THIS edge -- i.e. its own primary key is one of the anchor-
            # side join columns.
            join_columns_on_anchor = {c1 for c1, _c2 in join_ab}
            if not (anchor.primary_key_columns and anchor.primary_key_columns[0] in join_columns_on_anchor):
                continue

            display_a = _display_name(anchor.object_fqn, anchor.document.metadata)
            display_b_plural = _pluralize(_display_name(info_b.object_fqn, info_b.document.metadata))
            on_ab = _on_clause("t1", "t2", join_ab)
            join_path_ab = tuple(f"{anchor.object_fqn}.{c1} -> {info_b.object_fqn}.{c2}" for c1, c2 in join_ab)
            rendered_anchor = _render_object_reference(anchor.object_fqn)
            rendered_b = _render_object_reference(info_b.object_fqn)
            anchor_group_column = anchor.primary_key_columns[0]
            rendered_group_column = _qualified_column("t1", anchor_group_column)

            yield _CandidateAttempt(
                pattern=PATTERN_AGGREGATE_OVER_RELATIONSHIP, family=FAMILY_AGGREGATE_OVER_RELATIONSHIP,
                sql=(
                    f"SELECT {rendered_group_column}, COUNT(*) AS RowCount FROM {rendered_anchor} AS t1 "
                    f"JOIN {rendered_b} AS t2 ON {on_ab} GROUP BY {rendered_group_column}"
                ),
                referenced_object_fqns=(anchor.object_fqn, info_b.object_fqn),
                question=f"How many {display_b_plural} are there for each {display_a}?",
                result_shape=RESULT_SHAPE_GROUPED_AGGREGATION, join_path=join_path_ab,
                anchor_object_fqns=(anchor.object_fqn, info_b.object_fqn), involved=(anchor, info_b),
            )


def _three_table_relationship_path_attempts(
    state: _GenerationState, obj_by_lower: Mapping[str, _ObjectInfo], graph: RelationshipGraph,
    ordered_objects: Sequence[_ObjectInfo], max_relationship_path_length: int,
) -> Iterator[_CandidateAttempt]:
    if max_relationship_path_length < 2:
        return

    seen_path_keys: set[tuple[str, str, str]] = set()

    for anchor in ordered_objects:
        anchor_lower = anchor.object_fqn.lower()
        for b_lower in sorted(graph.adjacency.get(anchor_lower, ())):
            info_b = obj_by_lower.get(b_lower)
            if info_b is None:
                continue
            join_ab = _confirmed_join_columns(obj_by_lower, anchor.object_fqn, info_b.object_fqn)
            if not join_ab:
                state.warn_missing_join_columns_once(
                    ("AB", anchor_lower, b_lower),
                    f"skipped relationship {anchor.object_fqn!r} -> {info_b.object_fqn!r}: "
                    f"no usable confirmed join column found",
                )
                continue

            display_a = _display_name(anchor.object_fqn, anchor.document.metadata)
            display_b_plural = _pluralize(_display_name(info_b.object_fqn, info_b.document.metadata))
            on_ab = _on_clause("t1", "t2", join_ab)
            join_path_ab = tuple(f"{anchor.object_fqn}.{c1} -> {info_b.object_fqn}.{c2}" for c1, c2 in join_ab)
            projection_a = _projection_columns(anchor, limit=2)
            projection_b = _projection_columns(info_b, limit=2)
            rendered_anchor = _render_object_reference(anchor.object_fqn)
            rendered_b = _render_object_reference(info_b.object_fqn)

            for c_lower in sorted(graph.adjacency.get(b_lower, ())):
                if c_lower in (anchor_lower, b_lower):
                    continue  # cycle prevention: never back to the anchor, never staying at the bridge
                path_key = (anchor_lower, b_lower, c_lower)
                if path_key in seen_path_keys:
                    continue  # duplicate-path prevention: this exact (anchor, bridge, target) triple already yielded
                seen_path_keys.add(path_key)

                info_c = obj_by_lower.get(c_lower)
                if info_c is None:
                    continue
                join_bc = _confirmed_join_columns(obj_by_lower, info_b.object_fqn, info_c.object_fqn)
                if not join_bc:
                    state.warn_missing_join_columns_once(
                        ("BC", anchor_lower, b_lower, c_lower),
                        f"skipped relationship path {anchor.object_fqn!r} -> {info_b.object_fqn!r} -> "
                        f"{info_c.object_fqn!r}: no usable confirmed join column found for the second hop",
                    )
                    continue

                display_c_plural = _pluralize(_display_name(info_c.object_fqn, info_c.document.metadata))
                on_bc = _on_clause("t2", "t3", join_bc)
                join_path_abc = join_path_ab + tuple(
                    f"{info_b.object_fqn}.{c1} -> {info_c.object_fqn}.{c2}" for c1, c2 in join_bc
                )
                projection_c = _projection_columns(info_c, limit=2)
                select_list_abc = ", ".join(
                    [_qualified_column("t1", c) for c in projection_a] + [_qualified_column("t2", c) for c in projection_b]
                    + [_qualified_column("t3", c) for c in projection_c]
                )
                rendered_c = _render_object_reference(info_c.object_fqn)
                yield _CandidateAttempt(
                    pattern=PATTERN_THREE_TABLE_RELATIONSHIP_PATH, family=FAMILY_THREE_TABLE_PATH,
                    sql=(
                        f"SELECT TOP {_TOP_N_LIST_ROWS} {select_list_abc} FROM {rendered_anchor} AS t1 "
                        f"JOIN {rendered_b} AS t2 ON {on_ab} "
                        f"JOIN {rendered_c} AS t3 ON {on_bc}"
                    ),
                    referenced_object_fqns=(anchor.object_fqn, info_b.object_fqn, info_c.object_fqn),
                    question=f"For each {display_a}, show the related {display_b_plural} and {display_c_plural}.",
                    result_shape=RESULT_SHAPE_LIST, join_path=join_path_abc,
                    anchor_object_fqns=(anchor.object_fqn, info_b.object_fqn, info_c.object_fqn),
                    involved=(anchor, info_b, info_c),
                )


# ── Streaming, family-balanced selection (Step 11A2) ───────────────────────


class _FamilyStream:
    """Wraps one family's raw, lazy attempt iterator plus the shared
    `_GenerationState`, exposing pull() -> GeneratedSqlExample | None.
    `exhausted` becomes (and permanently stays) True only once the
    UNDERLYING attempt iterator itself has genuinely run out — never
    merely because the shared attempt budget was hit, which is a
    transient, whole-run condition checked separately by the caller
    (state.attempt_limit_reached), not a property of this one family."""

    __slots__ = ("family", "_state", "_raw_attempts", "exhausted")

    def __init__(self, family: str, state: _GenerationState, raw_attempts: Iterator[_CandidateAttempt]) -> None:
        self.family = family
        self._state = state
        self._raw_attempts = raw_attempts
        self.exhausted = False

    def pull(self) -> GeneratedSqlExample | None:
        """Pulls and validates raw attempts from the underlying iterator,
        one at a time, until either a validated candidate is produced
        (returned), the iterator itself runs dry (`exhausted` set, returns
        None), or the shared attempt budget is exhausted (returns None,
        `exhausted` left False). Never materializes more than the ONE
        attempt currently being considered."""
        if self.exhausted:
            return None
        while True:
            if self._state.attempt_limit_reached:
                return None
            attempt = next(self._raw_attempts, None)
            if attempt is None:
                self.exhausted = True
                return None
            validated = self._state.try_validate(attempt)
            if validated is not None:
                return validated
            # else: rejected for a duplicate/validation/trust reason --
            # already recorded by try_validate; keep pulling within budget.


def _stream_select_balanced(
    state: _GenerationState, family_streams: Mapping[str, _FamilyStream],
    *, max_total_examples: int, max_examples_per_object: int,
) -> tuple[list[GeneratedSqlExample], tuple[str, ...]]:
    """
    Deterministic, family-balanced streaming round-robin — the direct
    streaming analogue of Step 11A1's _select_balanced, but pulling ONE
    candidate at a time from each family's live stream instead of a pre-
    built pool. Cycles through _FAMILY_ORDER (fixed, generic — requirement
    5) taking AT MOST ONE accepted candidate per family per pass, so an
    early-listed family can never consume the entire global allowance
    while a later family's stream still has eligible candidates (
    requirement 2). A family whose stream is exhausted (or whose every
    remaining candidate this pass is object-capped and the stream itself
    then runs dry) drops out of rotation; its share of every subsequent
    pass is implicitly redistributed to whichever families remain.

    The per-object cap is tracked in ONE shared counter across all four
    families — a hub object already at cap from e.g. TWO_TABLE_JOIN
    candidates can no longer receive an AGGREGATE_OVER_RELATIONSHIP
    candidate either (hub-domination protection, preserved from Step
    11A1).

    Stops the moment max_total_examples is reached, every family's stream
    is exhausted, OR state.attempt_limit_reached — whichever comes first;
    the outer generate_trusted_examples() caller distinguishes these via
    state.attempt_limit_reached and the returned exhausted_families tuple.

    Returns (selected, exhausted_families) — `selected` has length exactly
    min(max_total_examples, total realistically selectable candidates
    given every object cap AND the attempt budget).
    """
    selected: list[GeneratedSqlExample] = []
    per_object_counts: dict[str, int] = {}

    active = list(_FAMILY_ORDER)
    while active and len(selected) < max_total_examples and not state.attempt_limit_reached:
        for family in list(active):
            if len(selected) >= max_total_examples or state.attempt_limit_reached:
                break
            stream = family_streams[family]
            picked: GeneratedSqlExample | None = None
            while True:
                candidate = stream.pull()
                if candidate is None:
                    break
                capped_on = [
                    fqn for fqn in candidate.anchor_object_fqns
                    if per_object_counts.get(fqn.lower(), 0) >= max_examples_per_object
                ]
                if capped_on:
                    state.reject_for_object_cap(candidate, capped_on, max_examples_per_object)
                    continue  # try the next candidate from the SAME family's stream, within this turn
                picked = candidate
                break

            if picked is None:
                if stream.exhausted:
                    active.remove(family)
                continue

            selected.append(picked)
            for fqn in picked.anchor_object_fqns:
                key = fqn.lower()
                per_object_counts[key] = per_object_counts.get(key, 0) + 1
            if stream.exhausted:
                active.remove(family)

    exhausted_families = tuple(sorted(family for family, stream in family_streams.items() if stream.exhausted))
    return selected, exhausted_families


# ── Public entry point ──────────────────────────────────────────────────────


def generate_trusted_examples(
    documents: Sequence[ContextDocument],
    *,
    source_id: int,
    build_id: int,
    schema_fingerprint: str,
    max_total_examples: int = SQL_EXAMPLE_GENERATION_MAX_TOTAL_EXAMPLES,
    max_examples_per_object: int = SQL_EXAMPLE_GENERATION_MAX_EXAMPLES_PER_OBJECT,
    max_relationship_path_length: int = SQL_EXAMPLE_GENERATION_MAX_RELATIONSHIP_PATH_LENGTH,
    max_candidate_attempts: int = SQL_EXAMPLE_GENERATION_MAX_CANDIDATE_ATTEMPTS,
) -> SqlExampleGenerationResult:
    """
    Deterministically generate a bounded set of PROVENANCE_SCHEMA_
    GENERATED_TEMPLATE trusted example candidates from `documents` (any
    sequence of ContextDocument-shaped retrieval documents — e.g.
    SchemaContextResult.documents or an IndexedSchemaDocument sequence)
    and their own structured confirmed-relationship metadata.

    Never inserts anything (core.sqlservice.sql_example_store is never
    imported), never computes an embedding, never opens a connection of
    any kind. Every returned candidate has already passed a REAL
    validate_sql_candidate() call and a REAL build_trusted_example_
    candidate() call — see _GenerationState.try_validate.

    Step 11A2 streaming pipeline — see this module's own docstring for the
    full rationale and the Memory = O(...) bound:
      1. Build the (cheap, in-memory-only) schema index once: eligible
         objects (_eligible_objects) and the confirmed-relationship graph
         (build_relationship_graph) — this is `schema_index` in the memory
         bound, scaling with the caller-supplied `documents` themselves.
      2. Construct four independent, lazy per-family attempt iterators —
         never a materialized pool.
      3. _stream_select_balanced() round-robins across the four live
         streams, pulling and validating ONE candidate attempt at a time,
         enforcing max_total_examples, max_examples_per_object, AND (new)
         max_candidate_attempts, stopping the instant any one of the three
         is satisfied.

    Deterministic: identical `documents` (regardless of input ORDER —
    every object/relationship/column iteration order below is sorted)
    always produces byte-identical output, including candidate iteration
    order — _FAMILY_ORDER is a fixed constant, never input-dependent, and
    every per-family stream's own internal traversal order is likewise
    input-order-independent (sorted object_fqn / sorted adjacency).

    Raises ValueError for invalid limit configuration (a caller/programmer
    contract violation, never a routine condition) — mirrors every other
    *_candidate/*_validate limit-checking convention in this codebase.
    """
    for name, value in (
        ("max_total_examples", max_total_examples), ("max_examples_per_object", max_examples_per_object),
        ("max_relationship_path_length", max_relationship_path_length), ("max_candidate_attempts", max_candidate_attempts),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")
    if max_relationship_path_length > MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH:
        raise ValueError(
            f"max_relationship_path_length={max_relationship_path_length} exceeds the hard cap of "
            f"{MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH} edges"
        )
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise ValueError(f"source_id must be a positive integer, got {source_id!r}")
    if not isinstance(build_id, int) or isinstance(build_id, bool) or build_id <= 0:
        raise ValueError(f"build_id must be a positive integer, got {build_id!r}")
    if not isinstance(schema_fingerprint, str) or not schema_fingerprint.strip():
        raise ValueError("schema_fingerprint must be a non-empty string")

    state = _GenerationState(
        source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        max_candidate_attempts=max_candidate_attempts,
    )

    eligible_objects, eligibility_warnings = _eligible_objects(documents)
    for warning in eligibility_warnings:
        state._add_warning(warning)
    obj_by_lower = {info.object_fqn.lower(): info for info in eligible_objects}
    graph = build_relationship_graph([info.document for info in eligible_objects])

    family_streams: dict[str, _FamilyStream] = {
        FAMILY_SINGLE_TABLE: _FamilyStream(
            FAMILY_SINGLE_TABLE, state, _single_table_family_stream(eligible_objects),
        ),
        FAMILY_TWO_TABLE_JOIN: _FamilyStream(
            FAMILY_TWO_TABLE_JOIN, state, _two_table_join_attempts(state, obj_by_lower, graph, eligible_objects),
        ),
        FAMILY_THREE_TABLE_PATH: _FamilyStream(
            FAMILY_THREE_TABLE_PATH, state,
            _three_table_relationship_path_attempts(state, obj_by_lower, graph, eligible_objects, max_relationship_path_length),
        ),
        FAMILY_AGGREGATE_OVER_RELATIONSHIP: _FamilyStream(
            FAMILY_AGGREGATE_OVER_RELATIONSHIP, state,
            _aggregate_over_relationship_attempts(state, obj_by_lower, graph, eligible_objects),
        ),
    }

    selected, exhausted_families = _stream_select_balanced(
        state, family_streams, max_total_examples=max_total_examples, max_examples_per_object=max_examples_per_object,
    )

    if state.attempt_limit_reached:
        # Requirement 3: never continue scanning silently -- one clear,
        # deterministic warning, ALWAYS present (appended directly,
        # bypassing the general _MAX_RETAINED_WARNINGS bound -- this
        # single, one-time status line is never subject to the "many
        # per-attempt warnings" volume the cap protects against, and must
        # never be silently dropped merely because other warnings already
        # filled that budget), reporting exactly how many examples were
        # accepted before the ceiling stopped the scan.
        state.warnings.append(
            f"candidate attempt limit ({max_candidate_attempts}) reached before the output limit "
            f"({max_total_examples}) or every pattern family was exhausted -- stopped deterministically "
            f"with {len(selected)} example(s) accepted so far"
        )

    selected_counts_by_pattern: Counter = Counter(g.pattern for g in selected)
    rejected_total = sum(state.rejected_counts_by_reason.values())

    return SqlExampleGenerationResult(
        source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        candidates=tuple(selected), generated_count=len(selected), skipped_count=rejected_total,
        counts_by_pattern=MappingProxyType(dict(selected_counts_by_pattern)),
        candidate_attempt_count=state.attempt_count,
        attempt_limit=max_candidate_attempts,
        attempt_limit_reached=state.attempt_limit_reached,
        rejected_counts_by_reason=MappingProxyType(dict(state.rejected_counts_by_reason)),
        exhausted_families=exhausted_families,
        warnings=tuple(state.warnings),
    )
