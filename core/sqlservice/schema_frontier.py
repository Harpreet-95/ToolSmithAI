"""
Broad, compact candidate frontier for the AI schema-selection stage — SQL
rebuild Phase 7, Step 3C, Part 2.

Sits between core.sqlservice.schema_context_retrieval's existing hybrid
retrieval computation and its final ~top_k context packing. Reuses the SAME
building blocks that module already uses (core.sqlservice.vector_index,
core.sqlservice.lexical_index, core.sqlservice.business_concept_index,
core.sqlservice.canonical_scoring, and the identical RRF_K/DEFAULT_ALLOWED_
TIERS/weight constants) rather than importing schema_context_retrieval's own
private helpers — the same "small validators/helpers live per-module, never
a shared private import" convention core.sqlservice.relationship_graph and
core.sqlservice.sql_generation_service already establish (see each of their
own docstrings). Zero import-cycle risk, zero behavior coupling to
schema_context_retrieval's own top_k/candidate_pool_size cutoffs.

Produces a bounded, deterministic set of IMMUTABLE compact summaries (never
full document prose, raw rows, credentials, embeddings, prompts, or
untrusted inactive semantic claims) — enough for an AI schema-selection call
to reason over up to ~300 candidate business objects without seeing (or
being able to leak) anything beyond bounded, already-trusted structured
metadata.

Phase 7, Step 3C1 (admission-algorithm correction): candidates are admitted
by taking each active ranking lane's (semantic/lexical/business-concept) OWN
top `per_lane_top_n` (default 100) INDEPENDENTLY, then deduplicating the
union — never a shared, lockstep-advancing per-round budget across lanes.
Gate A diagnosis (2026-09-21) found the prior lockstep round-robin design
capped each lane's REACHABLE depth at roughly frontier_size/num_active_lanes
(~33 with 3 lanes), so an object ranked, say, 56th in ONE lane but poorly in
the other two could never be admitted even though 56 <= 100. Taking each
lane's top 100 independently and unioning (deduplicated, capped at
`max_admitted_candidates`, default 300 = 100 * 3 lanes, so no lane's own
top-100 is ever silently cut off by that cap) fixes this by construction.
The FINAL character-budget pass (`max_frontier_characters`) is the only
place candidates can still be dropped, and it always does so from the tail
of the fused-score-ordered list, with an explicit warning naming what was
cut — never silent, and never because of the per-lane admission step.

Phase 7, Step 5 audit fix: a VERIFIED business-knowledge mapping (owner-
confirmed ground truth, never a heuristic similarity score) force-admits its
object independently of all three lanes, and sorts ahead of every fused-score
candidate that lacks one — so an object whose real name/columns share little
vocabulary with the question (e.g. an internally-named payment table a
business user only ever calls by an abbreviation) still reaches the selector
on the strength of that confirmed mapping alone, and survives the character
budget instead of being silently trimmed from the tail. UNRESOLVED mappings
are not strong enough evidence for this and are unaffected — they still only
ever attach to an object admitted through the normal lanes.

Scope: never calls an AI provider, never generates or validates SQL, never
executes anything, never logs or persists the question text. Makes exactly
ONE embedding call (provider.embed_batch([question])) — the SAME one-
embedding-per-question budget core.sqlservice.schema_context_retrieval
itself uses; a Step 3C caller building both a frontier AND the legacy
top-25 context in the same request would need two calls, but
the AI-first fast path never does both, so the whole pipeline stays at
one embedding call total.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from core.config import BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED
from core.sqlservice.business_concept_index import (
    MatchedConceptEvidence,
    load_business_concept_index,
    search_business_concept_index,
)
from core.sqlservice.business_knowledge_evidence import (
    BusinessKnowledgeMappingEvidence,
    load_business_knowledge_evidence,
)
from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, load_canonical_scores
from core.sqlservice.embedding_providers import EmbeddingProvider, ProviderEmbeddingBatchResponse
from core.sqlservice.lexical_index import LexicalIndexIdentity, load_lexical_index, search_lexical_index, tokenize
from core.sqlservice.retrieval_store import get_retrieval_build
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_CANONICAL_TABLE,
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
    RESTRICTION_STATUS_NONE,
    SchemaObjectAuthority,
    load_schema_object_authority,
)
from core.sqlservice.snapshot_classifier import (
    SNAPSHOT_CLASS_CONFIRMED,
    SNAPSHOT_CLASS_UNCERTAIN,
    load_snapshot_classification,
)
from core.sqlservice.schema_context_retrieval import (
    DEFAULT_ALLOWED_TIERS,
    DEFAULT_BUSINESS_CONCEPT_WEIGHT,
    DEFAULT_CANONICAL_PRIOR_WEIGHT,
    DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL,
    DEFAULT_ENABLE_CANONICAL_PRIOR,
    RRF_K,
)
from core.sqlservice.vector_index import (
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_INCOMPLETE,
    STATUS_INVALID,
    STATUS_OK,
    STATUS_UNOWNED,
    IndexedSchemaDocument,
    load_vector_index,
    search_vector_index,
)

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "STATUS_BUILD_NOT_FOUND",
    "STATUS_BUILD_NOT_READY",
    "STATUS_INCOMPLETE",
    "STATUS_INVALID",
    "DEFAULT_FRONTIER_SIZE",
    "DEFAULT_MAX_ADMITTED_CANDIDATES",
    "DEFAULT_MAX_FRONTIER_CHARACTERS",
    "DEFAULT_MAX_CHARACTERS_PER_OBJECT",
    "DEFAULT_MAX_ALIASES_PER_OBJECT",
    "DEFAULT_MAX_COLUMNS_PER_CATEGORY",
    "DEFAULT_MAX_RELATIONSHIPS_PER_OBJECT",
    "DEFAULT_MAX_CONCEPTS_PER_OBJECT",
    "LANE_SEMANTIC",
    "LANE_LEXICAL",
    "LANE_BUSINESS_CONCEPT",
    "SchemaFrontierError",
    "SchemaFrontierValidationError",
    "FrontierCandidate",
    "SchemaFrontierResult",
    "render_frontier_candidate",
    "build_schema_frontier",
]

# ── Defaults (all caller-overridable, all env-backed via core.config) ─────

# Phase 7, Step 3C1: the number of top-ranked candidates taken INDEPENDENTLY
# from EACH active lane (semantic/lexical/business-concept) before the union
# — never a shared per-round budget. See build_schema_frontier's own
# docstring for the full rationale.
DEFAULT_FRONTIER_SIZE = 100
# Hard cap on the deduplicated union's own size. 300 = DEFAULT_FRONTIER_SIZE
# * 3 lanes — the worst case (zero overlap across all three lanes) exactly
# fits, so this cap can never silently truncate any lane's own top-100 in
# the current 3-lane design; it exists purely as a defensive ceiling.
DEFAULT_MAX_ADMITTED_CANDIDATES = 300
# Scaled for up to DEFAULT_MAX_ADMITTED_CANDIDATES compact candidates at up
# to DEFAULT_MAX_CHARACTERS_PER_OBJECT characters each (300 * 600 = 180000)
# — the character budget, not the per-lane admission step, is the only
# place a candidate can still be dropped (see its own warning contract).
DEFAULT_MAX_FRONTIER_CHARACTERS = 180000
DEFAULT_MAX_CHARACTERS_PER_OBJECT = 600
DEFAULT_MAX_ALIASES_PER_OBJECT = 8
DEFAULT_MAX_COLUMNS_PER_CATEGORY = 10
DEFAULT_MAX_RELATIONSHIPS_PER_OBJECT = 6
DEFAULT_MAX_CONCEPTS_PER_OBJECT = 6

_ALL_TIERS = frozenset({1, 2, 3, 4})
_MAX_QUESTION_CHARACTERS = 2000
_CONTEXT_SEPARATOR = "\n\n"

# Phase 7, Step 3J3 — deterministic, bounded multiplicative demotion for an
# UNCERTAIN_SNAPSHOT candidate's fused ranking score. Never zero (an
# uncertain candidate must remain reachable, never effectively excluded by
# the penalty alone) and never applied to a CONFIRMED_SNAPSHOT candidate
# (those are excluded from admission entirely, not merely demoted).
_UNCERTAIN_SNAPSHOT_RANK_PENALTY = 0.7

# Lane names — used for round-robin admission order and FrontierCandidate's
# own admitted_via_lanes provenance. Fixed, deterministic iteration order:
# semantic first (every tier-allowed document always has a semantic rank,
# so it anchors the frontier), then lexical, then business-concept.
LANE_SEMANTIC = "semantic"
LANE_LEXICAL = "lexical"
LANE_BUSINESS_CONCEPT = "business_concept"
_LANE_ORDER = (LANE_SEMANTIC, LANE_LEXICAL, LANE_BUSINESS_CONCEPT)


class SchemaFrontierError(Exception):
    """Base for exceptions this module raises itself."""


class SchemaFrontierValidationError(SchemaFrontierError):
    """The provider's single-item embedding response failed validation —
    identical category/contract to core.sqlservice.schema_context_
    retrieval.SchemaRetrievalValidationError, specialized to this module's
    own single-embedding call."""


# ── Question / parameter validation (duplicated per this codebase's
#    established per-module-validator convention — see core.sqlservice.
#    sql_generation_service._validate_question's own docstring) ───────────


def _validate_question(question: object) -> str:
    if not isinstance(question, str):
        raise ValueError("question must be a string")
    trimmed = question.strip()
    if not trimmed:
        raise ValueError("question must not be empty")
    if len(trimmed) > _MAX_QUESTION_CHARACTERS:
        raise ValueError(
            f"question length {len(trimmed)} exceeds the maximum of {_MAX_QUESTION_CHARACTERS} characters"
        )
    return trimmed


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


def _validate_single_embedding_response(
    response: ProviderEmbeddingBatchResponse, expected_model: str, expected_dimensions: int,
) -> tuple[float, ...]:
    items = response.items
    if len(items) != 1:
        raise SchemaFrontierValidationError(
            f"provider returned {len(items)} embeddings for a single-question request, expected exactly 1"
        )
    item = items[0]
    if not isinstance(item.index, int) or isinstance(item.index, bool) or item.index != 0:
        raise SchemaFrontierValidationError(f"provider returned index {item.index!r}, expected exactly 0")
    if response.model != expected_model:
        raise SchemaFrontierValidationError(
            f"provider response model {response.model!r} does not match the expected model {expected_model!r}"
        )
    vector = item.vector
    try:
        length = len(vector)
    except TypeError as exc:
        raise SchemaFrontierValidationError("provider returned a non-sequence vector") from exc
    if length != expected_dimensions:
        raise SchemaFrontierValidationError(f"provider returned {length} dimensions, expected {expected_dimensions}")
    floats: list[float] = []
    for i, raw in enumerate(vector):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise SchemaFrontierValidationError(f"vector element {i} is not a number: {raw!r}") from exc
        if math.isnan(value) or math.isinf(value):
            raise SchemaFrontierValidationError(f"vector element {i} is not finite: {value!r}")
        floats.append(value)
    norm = math.sqrt(sum(f * f for f in floats))
    if norm == 0.0:
        raise SchemaFrontierValidationError("provider returned a zero vector, which cannot be used as a query")
    return tuple(floats)


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrontierCandidate:
    """
    One bounded, immutable compact summary of a candidate business object —
    the ONLY shape the AI schema-selection stage (core.sqlservice.
    schema_selection_service) ever sees. Deliberately excludes full document
    prose, raw row samples, credentials, embeddings, and the query vector.

    `admitted_via_lanes` names which real-evidence ranking lane(s) — a
    subset of (LANE_SEMANTIC, LANE_LEXICAL, LANE_BUSINESS_CONCEPT) — this
    object actually appeared in; every candidate has at least one (the
    round-robin union admits an object only through real per-lane evidence,
    never canonical scoring alone — see build_schema_frontier's own
    docstring). `fused_rank`/`fused_score` are this object's 1-indexed
    position/score within THIS frontier's own final ordering (folding in the
    canonical prior exactly as core.sqlservice.schema_context_retrieval
    does, at the SAME bounded weight) — not the same numbering space as
    `semantic_rank`/`lexical_rank`/`business_concept_rank`, which are each
    lane's own global (tier-filtered, whole-build) 1-indexed rank.

    Phase 7, Step 3C1 (evidence-first correction, 2026-09-22):
    `local_foreign_key_columns` are this object's OWN column names that are
    the LOCAL side of a confirmed outbound relationship edge (core.
    sqlservice.retrieval_documents' "confirmed_relationship_edges" metadata,
    `direction == "outbound"`) — never inferred from `foreign_key_summary`'s
    own TARGET-side descriptors (those name a DIFFERENT object's column;
    treating one as local evidence could validate a column that does not
    exist on THIS object). Unlike `dimension_columns` (bounded — the
    "ordinary" catch-all column list), `primary_key_columns`,
    `local_foreign_key_columns`, `measure_columns`, `date_columns`, and
    `status_columns` are deliberately NEVER truncated: a real key/semantic
    column must remain citable regardless of how many plain descriptive
    columns a wide table happens to also have.

    Phase 7, Step 3E — bounded FACTUAL authority/scope evidence (core.
    sqlservice.schema_object_authority.SchemaObjectAuthority, computed once
    per build from stored semantic contracts + parsed view definitions/
    dependencies — never per question, never a new AI/embedding call).
    Every authority_* field below defaults to a neutral/unknown value so
    existing construction sites (tests, any caller predating Step 3E) are
    unaffected. See render_frontier_candidate's own AUTHORITY line for
    exactly what of this reaches the selector prompt — never the full view
    SQL these facts were derived from.
    """

    object_fqn: str
    object_type: str
    retrieval_tier: int
    business_name: str | None
    business_purpose: str | None
    domain: str | None
    aliases: tuple[str, ...]
    primary_key_columns: tuple[str, ...]
    foreign_key_summary: tuple[str, ...]
    local_foreign_key_columns: tuple[str, ...]
    measure_columns: tuple[str, ...]
    date_columns: tuple[str, ...]
    status_columns: tuple[str, ...]
    dimension_columns: tuple[str, ...]
    confirmed_relationship_neighbors: tuple[str, ...]
    semantic_rank: int | None
    semantic_score: float | None
    lexical_rank: int | None
    lexical_score: float | None
    business_concept_rank: int | None
    business_concept_score: float | None
    canonical_rank: int | None
    canonical_total_score: float | None
    fused_rank: int
    fused_score: float
    admitted_via_lanes: tuple[str, ...]
    matched_business_concepts: tuple[MatchedConceptEvidence, ...]
    authority_class: str = "UNKNOWN"
    authority_canonical_table_fqns: tuple[str, ...] = ()
    authority_is_declared_preferred_view: bool = False
    authority_restriction_status: str = "UNKNOWN"
    authority_restrictions: tuple[str, ...] = ()
    authority_grain_class: str = "UNKNOWN"
    authority_aggregation_status: str = "UNKNOWN"
    authority_row_preservation_status: str = "UNKNOWN"
    authority_safe_dependency_count: int = 0
    # Phase 7, Step 6E3B1, Part 1 — `authority_unsafe_dependency_fqns` is the
    # GATE-CONSULTED field (core.sqlservice.schema_selection_service.
    # _validate_authority_facts / core.sqlservice.schema_selection_verifier.
    # _check_forbidden_authority both reject on `authority_class ==
    # UNSAFE_DEPENDENCY or authority_unsafe_dependency_fqns`) — it is
    # CLEARED to () only for a view this build's FRAMEWORK_BACKED_BUSINESS_
    # VIEW refinement (see below) actually applies to, so those two
    # pre-existing, unmodified gates stop rejecting it. The ORIGINAL
    # evidence is never lost: authority_unsafe_dependency_evidence_fqns
    # always holds the raw core.sqlservice.schema_object_authority result,
    # unconditionally, even when the gate-consulted field above is cleared.
    # Identical to authority_unsafe_dependency_fqns (and authority_class
    # stays AUTHORITY_CLASS_UNSAFE_DEPENDENCY) for every object this
    # refinement does not apply to — zero behavior change for any existing
    # caller/test predating this field.
    authority_unsafe_dependency_fqns: tuple[str, ...] = ()
    authority_unsafe_dependency_evidence_fqns: tuple[str, ...] = ()
    authority_evidence: tuple[str, ...] = ()
    # Phase 7, Step 3F, Part 2 — the entity name(s) (never present for a
    # plain CANONICAL_TABLE record) this object is DECLARED preferred for;
    # kept distinct from authority_canonical_table_fqns so family-collapse
    # grouping can key on it without conflating "declared preferred for X"
    # with "structurally reaches canonical table X".
    authority_preferred_view_for_entities: tuple[str, ...] = ()
    # Bounded, sorted FQNs of proven canonical/equivalent alternatives that
    # were collapsed INTO this surviving representative (see
    # _collapse_authority_family_representatives) — ranking provenance,
    # never silently discarded (Part 2, requirement 7). Empty for every
    # object that was not chosen as a family representative.
    authority_collapsed_member_fqns: tuple[str, ...] = ()
    # Phase 7, Step 3J, Part 5 — bounded, immutable business-knowledge-
    # package evidence (core.sqlservice.business_knowledge_evidence, itself
    # sourced from core.sqlservice.business_knowledge_mapper's
    # deterministically-verified mappings; never a raw AI claim). Empty for
    # every source without an ACTIVE business-knowledge package or with
    # core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED off -- zero behavior
    # change for those, exactly like matched_business_concepts/authority_*
    # default to a neutral empty value for any construction site predating
    # this field.
    business_knowledge_mappings: tuple[BusinessKnowledgeMappingEvidence, ...] = ()
    # Phase 7, Step 3J3 — advisory-only fields (a CONFIRMED_SNAPSHOT object
    # never reaches this dataclass at all; only NOT_A_SNAPSHOT [both fields
    # default/empty] or UNCERTAIN_SNAPSHOT populate these). Never affects
    # admission -- only this candidate's own rendered evidence and, at
    # ranking time, a bounded multiplicative demotion (see
    # _UNCERTAIN_SNAPSHOT_RANK_PENALTY).
    snapshot_uncertain: bool = False
    snapshot_confidence: float = 0.0
    snapshot_canonical_sibling_fqn: str | None = None
    snapshot_reason: str | None = None
    # Phase 7, Step 6C — this object's own confirmed-relationship join keys,
    # rendered as "local_column -> other_object.other_column" pairs (both
    # outbound and inbound edges; never a guessed/shared-name pair). Sourced
    # from the SAME "confirmed_relationship_edges" metadata local_foreign_
    # key_columns/confirmed_relationship_neighbors already read above —
    # never a new retrieval system, only a fuller rendering of data this
    # module already loads. Unbounded like primary_key_columns/local_
    # foreign_key_columns (a real join key must never be silently dropped).
    # Empty tuple for every existing construction site (tests, any caller
    # predating Step 6C) — zero behavior change to render_frontier_
    # candidate's own output, which never reads this field.
    confirmed_relationship_column_pairs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaFrontierResult:
    """Result of one build_schema_frontier() call. Deliberately carries no
    query_vector field — same encapsulation contract as core.sqlservice.
    schema_context_retrieval.SchemaContextResult's own docstring explains."""

    status: str
    build_id: int
    schema_fingerprint: str | None
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    candidates: tuple[FrontierCandidate, ...]
    total_characters: int
    truncated: bool
    warnings: tuple[str, ...]
    error: str | None
    # Phase 7, Step 3F, Part 2 observability — the admitted-candidate count
    # BEFORE authority-family collapse (never affects ranking/admission
    # itself; purely so a caller can report how many proven-redundant
    # alternatives were removed without recomputing anything). Equal to
    # len(candidates) whenever nothing was collapsed for this question.
    admitted_before_authority_collapse: int = 0


def _empty_result(status: str, build_id: int, provider: EmbeddingProvider, error: str | None) -> SchemaFrontierResult:
    return SchemaFrontierResult(
        status=status, build_id=build_id, schema_fingerprint=None,
        embedding_provider=provider.provider_id, embedding_model=provider.model,
        embedding_dimensions=provider.dimensions, candidates=(), total_characters=0,
        truncated=False, warnings=(), error=error,
    )


# ── Compact per-object rendering (structured metadata only, never invented;
#    used both for this module's own character-budget enforcement and by
#    core.sqlservice.schema_selection_service's prompt construction) ───────


def _format_authority_detail(candidate: FrontierCandidate) -> str | None:
    """One bounded, compact line summarizing every authority/scope fact
    beyond the bare authority_class itself — None when there is nothing
    non-trivial to add (the common case for an ordinary Tier 1/2 table with
    no special evidence), so a plain table's rendering stays exactly as
    terse as it always has. Never includes raw view SQL (see
    core.sqlservice.schema_object_authority's own module docstring for why
    that boundary matters) — only the already-bounded, already-typed facts
    that module computed."""
    parts: list[str] = []
    if candidate.authority_canonical_table_fqns:
        parts.append(f"canonical={','.join(candidate.authority_canonical_table_fqns)}")
    if candidate.authority_is_declared_preferred_view:
        parts.append("preferred_view=yes")
    if candidate.authority_restriction_status not in ("UNKNOWN", RESTRICTION_STATUS_NONE):
        kinds = f"({','.join(candidate.authority_restrictions)})" if candidate.authority_restrictions else ""
        parts.append(f"restriction={candidate.authority_restriction_status}{kinds}")
    if candidate.authority_grain_class not in ("UNKNOWN", "BASE_ROW"):
        parts.append(f"grain={candidate.authority_grain_class}")
    if candidate.authority_aggregation_status not in ("UNKNOWN", "NONE"):
        parts.append(f"agg={candidate.authority_aggregation_status}")
    if candidate.authority_row_preservation_status not in ("UNKNOWN", "PRESERVED"):
        parts.append(f"row_preservation={candidate.authority_row_preservation_status}")
    if candidate.authority_unsafe_dependency_evidence_fqns:
        parts.append(
            f"deps={candidate.authority_safe_dependency_count}safe/"
            f"{len(candidate.authority_unsafe_dependency_evidence_fqns)}unsafe"
            f"[{','.join(candidate.authority_unsafe_dependency_evidence_fqns)}]"
        )
        if candidate.authority_class == AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW:
            # The unsafe-dependency evidence above is preserved and shown,
            # but this object was refined past the blanket UNSAFE_DEPENDENCY
            # verdict -- make that explicit so a reader never mistakes the
            # "deps=...unsafe[...]" line above for an unresolved rejection.
            parts.append("framework_backed=true(original=UNSAFE_DEPENDENCY,lineage_verified,no_secret_columns)")
    if candidate.authority_evidence:
        parts.append(f"evidence={','.join(candidate.authority_evidence)}")
    return " ".join(parts) if parts else None


def render_frontier_candidate(candidate: FrontierCandidate) -> str:
    """A deterministic, compact text rendering of one FrontierCandidate,
    built only from its own already-bounded fields — never a truncation of
    the original full document text."""
    lines = [
        f"OBJECT: {candidate.object_fqn}",
        f"TYPE: {candidate.object_type}",
        f"TIER: {candidate.retrieval_tier}",
        f"AUTHORITY: {candidate.authority_class}",
    ]
    authority_detail = _format_authority_detail(candidate)
    if authority_detail:
        lines.append(f"AUTHORITY_DETAIL: {authority_detail}")
    if candidate.authority_collapsed_member_fqns:
        lines.append(f"AUTHORITY_COLLAPSED: {', '.join(candidate.authority_collapsed_member_fqns)}")
    if candidate.business_name:
        lines.append(f"BUSINESS_NAME: {candidate.business_name}")
    if candidate.business_purpose:
        lines.append(f"PURPOSE: {candidate.business_purpose}")
    if candidate.domain:
        lines.append(f"DOMAIN: {candidate.domain}")
    if candidate.aliases:
        lines.append(f"ALIASES: {', '.join(candidate.aliases)}")
    if candidate.primary_key_columns:
        lines.append(f"PRIMARY_KEY: {', '.join(candidate.primary_key_columns)}")
    if candidate.foreign_key_summary:
        lines.append(f"FOREIGN_KEYS: {', '.join(candidate.foreign_key_summary)}")
    if candidate.local_foreign_key_columns:
        lines.append(f"LOCAL_FOREIGN_KEY_COLUMNS: {', '.join(candidate.local_foreign_key_columns)}")
    if candidate.measure_columns:
        lines.append(f"MEASURE_COLUMNS: {', '.join(candidate.measure_columns)}")
    if candidate.date_columns:
        lines.append(f"DATE_COLUMNS: {', '.join(candidate.date_columns)}")
    if candidate.status_columns:
        lines.append(f"STATUS_COLUMNS: {', '.join(candidate.status_columns)}")
    if candidate.dimension_columns:
        lines.append(f"COLUMNS: {', '.join(candidate.dimension_columns)}")
    if candidate.confirmed_relationship_neighbors:
        lines.append(f"CONFIRMED_RELATIONSHIPS: {', '.join(candidate.confirmed_relationship_neighbors)}")
    if candidate.matched_business_concepts:
        concepts = ", ".join(sorted({e.concept for e in candidate.matched_business_concepts}))
        lines.append(f"MATCHED_CONCEPTS: {concepts}")
    if candidate.business_knowledge_mappings:
        # Phase 7, Step 3J1 fix -- compact by construction (never the raw
        # AI evidence text, which core.sqlservice.business_knowledge_
        # mapper/business_knowledge_evidence already bound in length but
        # this line never rendered in the first place): concept_id is
        # normally self-descriptive of its own concept_type (e.g.
        # "SQL_COURSE_FEE" already says PAYMENT_CONCEPT), so concept_type
        # is dropped from the render entirely to save space; mapping_status/
        # concept_origin are appended only when they differ from the
        # common case (VERIFIED, AUTHORITATIVE), never unconditionally.
        # This keeps a genuinely wide, many-column object (observed live:
        # a 24-column table) from having this evidence sacrificed by
        # _fit_to_character_budget's own DEFAULT_MAX_CHARACTERS_PER_OBJECT
        # before the object's ordinary column categories are even
        # considered for trimming.
        def _render_bk_entry(m: BusinessKnowledgeMappingEvidence) -> str:
            entry = f"{m.concept_id}:{m.mapping_role}({m.confidence:.2f})"
            if m.mapping_status != "VERIFIED":
                entry += f"/{m.mapping_status}"
            if m.concept_origin != "AUTHORITATIVE":
                entry += "/AI"
            return entry

        entries = ", ".join(_render_bk_entry(m) for m in candidate.business_knowledge_mappings)
        lines.append(f"BUSINESS_KNOWLEDGE: {entries}")
    if candidate.snapshot_uncertain:
        lines.append(
            f"UNCERTAIN_SNAPSHOT: possible historical snapshot of "
            f"{candidate.snapshot_canonical_sibling_fqn} (confidence={candidate.snapshot_confidence:.2f}) "
            "-- prefer the sibling unless this question specifically needs historical/point-in-time data"
        )
    return "\n".join(lines)


# Optional-section drop priority for enforcing DEFAULT_MAX_CHARACTERS_PER_
# OBJECT — lowest-value-for-selection-reasoning sections dropped first,
# never the identity/business-name/PK lines. Each entry is a field name on
# FrontierCandidate this function may clear (replace with ()) one at a time.
#
# Phase 7, Step 3F, Part 3 correction: dimension_columns/measure_columns/
# date_columns/status_columns are DELIBERATELY no longer in this list — an
# atomic whole-category clear (the pre-Step-3F behavior) let an entirely
# IRRELEVANT category (e.g. eight date columns nothing to do with the
# question) survive completely intact while a genuinely question-relevant
# column sitting in a lower-priority category (e.g. a real fee-amount
# column classified as an ordinary dimension column) was discarded whole —
# confirmed live: dbo.EDU_ClassSignups's own FeePaid column was silently
# dropped this way for a "total fees paid" question even though FeePaid
# was present in that object's own metadata all along. Those four
# categories are now trimmed by _trim_least_relevant_column below instead
# — one column at a time, globally least-question-relevant first, never
# touching primary_key_columns/local_foreign_key_columns at all (Part 3
# requirement 4 — always preserved, removed from this list too for the
# same reason).
_DROP_PRIORITY = (
    "matched_business_concepts", "confirmed_relationship_neighbors", "aliases",
    "foreign_key_summary",
    # Phase 7, Step 3E authority detail sub-fields — dropped (as a group,
    # evidence first) before measure/date/status/key columns, since those
    # remain the more foundational, uniquely-citable facts the selector's
    # own object_reasons.supporting_columns validation depends on. Clearing
    # all four collapses _format_authority_detail's own output to nothing
    # (AUTHORITY_DETAIL line disappears entirely) while the bare
    # AUTHORITY: <class> line — a single scalar field, never dropped —
    # always survives.
    "authority_evidence", "authority_unsafe_dependency_fqns",
    "authority_canonical_table_fqns", "authority_restrictions",
    # Step 3F, Part 2 — pure observability (which proven-redundant
    # alternatives this representative absorbed); safe to drop before ever
    # touching real column evidence.
    "authority_collapsed_member_fqns",
    # Phase 7, Step 3J, Part 5 — deliberately LAST: this is the evidence
    # that lets the selector distinguish e.g. a SQL-tuition object from an
    # IPLAN-installment-payment object, so it is preserved through every
    # other optional-section drop and only cleared as a last resort before
    # real column trimming begins.
    "business_knowledge_mappings",
    # Phase 7, Step 3J3 — a safety ADVISORY, not core identity/business
    # evidence; dropped only once everything else (including business
    # knowledge) has already been cleared and the object still does not
    # fit. Losing this line under extreme character pressure just means
    # the selector reasons about this one candidate without the hint --
    # it never affects admission, which already excluded every CONFIRMED
    # snapshot outright. Clearing the bool gate field (not the string
    # fields it gates) is deliberate: _replace_field always substitutes
    # `()`, and only the bool field's falsiness is ever branched on by
    # render_frontier_candidate -- clearing it is what actually suppresses
    # the UNCERTAIN_SNAPSHOT line.
    "snapshot_uncertain",
)

# The four column categories eligible for the relevance-aware progressive
# trim below (Phase 7, Step 3F, Part 3). primary_key_columns and
# local_foreign_key_columns are deliberately excluded — always preserved.
_SCORED_COLUMN_FIELDS: tuple[str, ...] = ("dimension_columns", "measure_columns", "date_columns", "status_columns")


def _column_token_overlap_score(column_name: str, query_tokens: frozenset[str]) -> int:
    """Bounded, generic lexical-overlap relevance score for one column name
    against the question's own tokens — reuses core.sqlservice.
    lexical_index.tokenize verbatim (the SAME case/underscore/punctuation/
    camelCase/singular-plural normalization already used for the lexical
    ranking lane), so "FeePaid" and a question containing "fee paid" share
    the tokens {"fee", "paid"} without any hardcoded column-name mapping.
    Pure, deterministic, question-dependent only via `query_tokens` — never
    cached (Part 3, requirement 12: computed fresh per request)."""
    if not query_tokens:
        return 0
    return len(set(tokenize(column_name)) & query_tokens)


def _order_columns_by_relevance(columns: Sequence[str], query_tokens: frozenset[str]) -> tuple[str, ...]:
    """Highest question-token-overlap first; alphabetical (case-insensitive)
    is used ONLY as the final, deterministic tie-break — never the primary
    ordering policy (Part 3, requirement 6). With `query_tokens` empty
    (e.g. an all-stopword question), every score is 0 and this degrades
    gracefully to the exact prior alphabetical-only behavior."""
    return tuple(sorted(columns, key=lambda c: (-_column_token_overlap_score(c, query_tokens), c.lower())))


def _trim_least_relevant_column(
    candidate: FrontierCandidate, query_tokens: frozenset[str],
) -> tuple[FrontierCandidate, bool]:
    """
    Removes exactly ONE column — the single least question-relevant column
    currently present across dimension_/measure_/date_/status_columns
    combined — and returns (updated_candidate, removed_a_relevant_column).

    Each of the four fields is already ordered most-to-least relevant (see
    _order_columns_by_relevance at construction time), so each field's own
    CURRENT tail is that field's own worst remaining member; comparing the
    four tails directly yields the true global minimum without needing to
    re-sort anything. Ties break on field name for full determinism.
    Returns (candidate, False) unchanged when all four fields are already
    empty — the caller stops looping in that case.
    """
    worst_field: str | None = None
    worst_score: int | None = None
    for field_name in _SCORED_COLUMN_FIELDS:
        values = getattr(candidate, field_name)
        if not values:
            continue
        score = _column_token_overlap_score(values[-1], query_tokens)
        if worst_score is None or score < worst_score or (score == worst_score and (worst_field is None or field_name < worst_field)):
            worst_field, worst_score = field_name, score
    if worst_field is None:
        return candidate, False
    removed_relevant = bool(worst_score and worst_score > 0)
    updated = _replace_field(candidate, worst_field, getattr(candidate, worst_field)[:-1])
    return updated, removed_relevant


def _fit_to_character_budget(
    candidate: FrontierCandidate, max_characters: int, query_tokens: frozenset[str] = frozenset(),
) -> tuple[FrontierCandidate, bool]:
    """Never invents or splits content mid-field — only progressively drops
    whole optional sections (see _DROP_PRIORITY), then trims real column
    evidence one column at a time (least question-relevant first — see
    _trim_least_relevant_column), then as a last resort truncates
    `business_purpose` with an explicit marker. Identity fields
    (object_fqn/object_type/retrieval_tier/business_name) are never
    dropped or truncated; primary_key_columns/local_foreign_key_columns are
    never dropped or trimmed at all (Part 3, requirement 4).

    Returns (candidate, column_summary_incomplete) — the second is True
    only when at least one column with a non-zero relevance score had to
    be trimmed to fit the budget (Part 3, requirement 10's own bounded
    observability signal; the caller surfaces this as a frontier-level
    warning, never per-column detail and never the question text itself).
    """
    current = candidate
    if len(render_frontier_candidate(current)) <= max_characters:
        return current, False
    for field_name in _DROP_PRIORITY:
        if len(render_frontier_candidate(current)) <= max_characters:
            break
        if getattr(current, field_name):
            current = _replace_field(current, field_name, ())
    column_summary_incomplete = False
    while len(render_frontier_candidate(current)) > max_characters:
        current, removed_relevant = _trim_least_relevant_column(current, query_tokens)
        if removed_relevant:
            column_summary_incomplete = True
        if not any(getattr(current, f) for f in _SCORED_COLUMN_FIELDS):
            break
    if len(render_frontier_candidate(current)) > max_characters and current.business_purpose:
        current = _replace_field(current, "business_purpose", None)
    if len(render_frontier_candidate(current)) > max_characters and current.business_name:
        overhead = len(render_frontier_candidate(_replace_field(current, "business_name", None)))
        # The re-inserted line always costs "\nBUSINESS_NAME: " + the name
        # itself, since OBJECT/TYPE/TIER lines always precede it.
        budget_for_name = max(0, max_characters - overhead - len("\nBUSINESS_NAME: "))
        truncated_name = current.business_name[:budget_for_name]
        current = _replace_field(current, "business_name", truncated_name or None)
    return current, column_summary_incomplete


def _replace_field(candidate: FrontierCandidate, field_name: str, value: object) -> FrontierCandidate:
    import dataclasses
    return dataclasses.replace(candidate, **{field_name: value})


# ── Independent per-lane union (deterministic; real evidence only) ────────
#
# Phase 7, Step 3C1. Replaces the prior lockstep round-robin (which advanced
# every active lane one rank per round, so with 3 active lanes the loop hit
# `frontier_size` total admissions after only ~frontier_size/3 rounds — a
# lane's own rank-34..100 candidates were structurally unreachable even
# though individually well within the intended top-100). Gate A's real-data
# diagnosis (2026-09-21) confirmed this: dbo.EDU_Track ranked far down
# on the business-concept lane alone for two golden questions — comfortably
# inside "top 100" — yet never reached the old frontier.


def _independent_lane_union(
    lane_rank_dicts: Mapping[str, Mapping[str, int]],
    fqn_by_key: Mapping[str, str],
    per_lane_top_n: int,
    max_admitted_candidates: int,
) -> tuple[list[str], bool]:
    """
    Takes each active lane's (LANE_SEMANTIC, LANE_LEXICAL, LANE_BUSINESS_
    CONCEPT, in that fixed order) own top `per_lane_top_n` candidates
    INDEPENDENTLY — a full, uninterrupted top-N per lane, never a shared
    per-round budget — then deduplicates the union (a key already admitted
    from an earlier lane is skipped, never double-counted or re-ranked by
    a later lane's own position for it). Every admitted key has real
    per-lane evidence by construction — an object with zero rank in every
    lane is never a candidate for admission here.

    Returns (admitted_keys, lane_capped): `lane_capped` is True only if
    `max_admitted_candidates` was reached WHILE a lane still had unexamined
    top-N candidates left — i.e. the union cap, not a lane's own top-N
    limit, cut something off. With the default max_admitted_candidates=300
    and 3 lanes at per_lane_top_n=100 each, the worst case (zero overlap)
    is exactly 300, so this is a defensive ceiling that should not trigger
    in the normal 3-lane configuration — but if it ever does, the caller is
    expected to surface it as an explicit warning (never silent).
    """
    lanes = [
        (name, sorted(ranks.items(), key=lambda kv: (kv[1], fqn_by_key[kv[0]]))[:per_lane_top_n])
        for name, ranks in ((n, lane_rank_dicts.get(n, {})) for n in _LANE_ORDER)
        if ranks
    ]
    admitted: list[str] = []
    seen: set[str] = set()
    lane_capped = False
    for _name, ordered in lanes:
        for key, _rank in ordered:
            if key in seen:
                continue
            if len(admitted) >= max_admitted_candidates:
                lane_capped = True
                break
            seen.add(key)
            admitted.append(key)
        if len(admitted) >= max_admitted_candidates:
            break
    return admitted, lane_capped


# ── Authority-family representative collapse (Phase 7, Step 3F, Part 2) ───
#
# Pure, deterministic post-admission filter. NOT business-intent selection:
# it removes only an object whose OWN structural equivalence to another
# already-admitted object was already PROVEN by core.sqlservice.
# schema_object_authority (a DECLARED_PREFERRED_VIEW or PROVEN_EQUIVALENT_
# VIEW record) — never two objects that merely look related. Runs once,
# after admission/fused-ranking, before any candidate is rendered; it never
# touches lane admission, ranking weights, or frontier_size.

_MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE = 5


def _collapse_authority_family_representatives(
    ordered_keys: Sequence[str],
    authority_by_fqn_lower: Mapping[str, "SchemaObjectAuthority"],
    fqn_by_key: Mapping[str, str],
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    """
    Groups admitted objects by business ENTITY NAME (never by shared name/
    column/dependency tokens alone — requirement 5) using ONLY the already-
    computed authority facts each object carries:

      - a CANONICAL_TABLE's own `canonical_table_fqns` names the entity(ies)
        it IS the canonical table for.
      - a DECLARED_PREFERRED_VIEW's own `preferred_view_for_entities` names
        the entity(ies) a trusted semantic contract explicitly designated
        it the preferred view for.
      - a PROVEN_EQUIVALENT_VIEW's own `canonical_table_fqns` names the
        entity(ies) its dependency closure was structurally proven
        equivalent to.

    For each entity whose canonical table IS present in `ordered_keys`:
      - if a declared preferred view for that SAME entity is also present,
        it becomes the representative; the canonical table and any OTHER
        preferred/equivalent view for that entity are suppressed.
      - else if a proven-equivalent view for that SAME entity is present,
        the canonical table remains the representative; the equivalent
        view(s) are suppressed.
      - else nothing is collapsed (a lone canonical table is not a family).

    CONDITIONALLY_COMPATIBLE_VIEW/RESTRICTED_VIEW/INCOMPATIBLE_GRAIN/
    UNSAFE_DEPENDENCY/UNKNOWN records are never inspected here at all — they
    structurally cannot enter `canonical_table_key_by_entity`/
    `preferred_view_keys_by_entity`/`equivalent_view_keys_by_entity` since
    only the three classes above ever populate those maps, so they can
    never be suppressed (requirement 4).

    Deterministic and independent of `ordered_keys`' own order: entities are
    processed in sorted (name) order, and a tie among multiple declared-
    preferred views for the same entity (rare) breaks on object_fqn
    (lexicographic) — never on admission/fused-rank order.

    Returns (collapsed_ordered_keys, suppressed_members_by_representative)
    — the second maps the SURVIVING representative's own lowercase key to a
    bounded (`_MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE`), sorted tuple of
    the real, original-case FQNs it absorbed (requirement 7 — ranking
    provenance is never silently discarded).
    """
    key_set = set(ordered_keys)
    canonical_table_key_by_entity: dict[str, str] = {}
    preferred_view_keys_by_entity: dict[str, list[str]] = {}
    equivalent_view_keys_by_entity: dict[str, list[str]] = {}

    for key in ordered_keys:
        authority = authority_by_fqn_lower.get(key)
        if authority is None:
            continue
        if authority.authority_class == AUTHORITY_CLASS_CANONICAL_TABLE:
            for entity in authority.canonical_table_fqns:
                canonical_table_key_by_entity.setdefault(entity, key)
        elif authority.authority_class == AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW:
            for entity in authority.preferred_view_for_entities:
                preferred_view_keys_by_entity.setdefault(entity, []).append(key)
        elif authority.authority_class == AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW:
            for entity in authority.canonical_table_fqns:
                equivalent_view_keys_by_entity.setdefault(entity, []).append(key)

    suppressed: set[str] = set()
    suppressed_members_by_representative: dict[str, list[str]] = {}

    for entity in sorted(canonical_table_key_by_entity):
        canonical_key = canonical_table_key_by_entity[entity]
        preferred_keys = sorted(k for k in preferred_view_keys_by_entity.get(entity, ()) if k in key_set)
        equivalent_keys = sorted(k for k in equivalent_view_keys_by_entity.get(entity, ()) if k in key_set)

        if preferred_keys:
            representative = preferred_keys[0]
            redundant = [canonical_key, *preferred_keys[1:], *equivalent_keys]
        elif equivalent_keys:
            representative = canonical_key
            redundant = list(equivalent_keys)
        else:
            continue

        for member_key in redundant:
            if member_key == representative or member_key in suppressed:
                continue
            suppressed.add(member_key)
            bucket = suppressed_members_by_representative.setdefault(representative, [])
            if len(bucket) < _MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE:
                bucket.append(member_key)

    collapsed_ordered_keys = [k for k in ordered_keys if k not in suppressed]
    suppressed_fqns_by_representative_key = {
        rep: tuple(sorted(fqn_by_key[m] for m in members))
        for rep, members in suppressed_members_by_representative.items()
    }
    return collapsed_ordered_keys, suppressed_fqns_by_representative_key


def _fuse(
    semantic_rank: int | None, lexical_rank: int | None, business_concept_rank: int | None,
    business_concept_weight: float, canonical_rank: int | None, canonical_weight: float,
) -> float:
    """Identical bounded RRF shape to core.sqlservice.schema_context_
    retrieval._fuse_ranks — reused only for the FINAL ordering of this
    frontier's already-admitted candidates, never for admission itself."""
    score = 0.0
    if semantic_rank is not None:
        score += 1.0 / (RRF_K + semantic_rank)
    if lexical_rank is not None:
        score += 1.0 / (RRF_K + lexical_rank)
    if business_concept_rank is not None and business_concept_weight:
        score += business_concept_weight / (RRF_K + business_concept_rank)
    if canonical_rank is not None and canonical_weight:
        score += canonical_weight / (RRF_K + canonical_rank)
    return score


def _relationship_neighbor_fqns(doc: IndexedSchemaDocument, *, limit: int) -> tuple[str, ...]:
    targets = doc.metadata.get("confirmed_relationship_targets") or ()
    sources = doc.metadata.get("confirmed_relationship_sources") or ()
    seen: list[str] = []
    seen_lower: set[str] = set()
    for descriptor in (*targets, *sources):
        object_fqn, sep, _column = str(descriptor).rpartition(".")
        if sep and object_fqn and object_fqn.lower() not in seen_lower and object_fqn.lower() != doc.object_fqn.lower():
            seen_lower.add(object_fqn.lower())
            seen.append(object_fqn)
        if len(seen) >= limit:
            break
    return tuple(sorted(seen))


def _confirmed_relationship_column_pairs(doc: IndexedSchemaDocument) -> tuple[str, ...]:
    """Phase 7, Step 6C — every real confirmed-relationship edge touching
    this object, rendered as an explicit "local_column -> other_object.
    other_column" pair (both directions: outbound edges use this object's
    own local column on the left; inbound edges use the OTHER object's
    column on the left, since that is the side actually named by the edge's
    own "other" descriptor — see core.sqlservice.retrieval_documents' own
    confirmed_relationship_edges construction for the exact
    {"direction", "column", "other"} shape). Reuses the SAME metadata
    _local_foreign_key_columns/_relationship_neighbor_fqns already read;
    never a second retrieval/graph computation. Sorted for determinism,
    de-duplicated, unbounded (a real join key must never be silently
    dropped)."""
    pairs: list[str] = []
    seen: set[str] = set()
    for edge in doc.metadata.get("confirmed_relationship_edges") or ():
        column = str(edge.get("column") or "").strip()
        other = str(edge.get("other") or "").strip()
        if not column or column == "*" or not other:
            continue
        if edge.get("direction") == "outbound":
            pair = f"{column} -> {other}"
        else:
            pair = f"{other} -> {doc.object_fqn}.{column}"
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return tuple(sorted(pairs))


def _local_foreign_key_columns(doc: IndexedSchemaDocument) -> tuple[str, ...]:
    """Every REAL, OWN column name that is the LOCAL side of a confirmed
    outbound relationship edge (core.sqlservice.retrieval_documents' own
    "confirmed_relationship_edges" metadata — the same kind-tagged,
    column-pair-verified field core.sqlservice.sql_validation already
    trusts for join-safety, requiring both a from_column and a to_column
    before a row is ever recorded at all). `direction == "outbound"` means
    THIS document owns the column; `direction == "inbound"` or a wildcard
    "*" column (a table-level CATALOG_VIEW_DEPENDENCY with no specific
    column) are excluded — this function never infers a local column from
    the TARGET side of any descriptor. Unbounded (a real key column must
    never be silently dropped for being alphabetically late)."""
    seen: list[str] = []
    seen_lower: set[str] = set()
    for edge in doc.metadata.get("confirmed_relationship_edges") or ():
        if edge.get("direction") != "outbound":
            continue
        column = str(edge.get("column") or "").strip()
        if not column or column == "*":
            continue
        if column.lower() not in seen_lower:
            seen_lower.add(column.lower())
            seen.append(column)
    return tuple(sorted(seen))


# ── Public entry point ────────────────────────────────────────────────────


def build_schema_frontier(
    question: str,
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    frontier_size: int = DEFAULT_FRONTIER_SIZE,
    max_admitted_candidates: int = DEFAULT_MAX_ADMITTED_CANDIDATES,
    max_frontier_characters: int = DEFAULT_MAX_FRONTIER_CHARACTERS,
    max_characters_per_object: int = DEFAULT_MAX_CHARACTERS_PER_OBJECT,
    allowed_tiers: frozenset[int] | None = None,
    enable_canonical_prior: bool = DEFAULT_ENABLE_CANONICAL_PRIOR,
    canonical_prior_weight: float = DEFAULT_CANONICAL_PRIOR_WEIGHT,
    enable_business_concept_retrieval: bool = DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL,
    business_concept_weight: float = DEFAULT_BUSINESS_CONCEPT_WEIGHT,
    max_aliases_per_object: int = DEFAULT_MAX_ALIASES_PER_OBJECT,
    max_columns_per_category: int = DEFAULT_MAX_COLUMNS_PER_CATEGORY,
    max_relationships_per_object: int = DEFAULT_MAX_RELATIONSHIPS_PER_OBJECT,
    max_concepts_per_object: int = DEFAULT_MAX_CONCEPTS_PER_OBJECT,
) -> SchemaFrontierResult:
    """
    Build a bounded, deterministic candidate frontier for `question` over
    one already-prepared build.

    Order of operations (cheapest/safest checks first):
      1. Validate the question and every numeric/tier parameter — raises
         ValueError, no I/O yet.
      2. load_vector_index() — the SAME ownership/READY/completeness/tier
         gate core.sqlservice.schema_context_retrieval uses. A non-OK
         status short-circuits here (zero provider calls).
      3. provider.embed_batch([question]) — exactly ONE call, one input —
         the single embedding this entire Step 3C pipeline spends.
      4. search_vector_index() over the whole tier-filtered index (semantic
         lane — every eligible document gets a rank here).
      5. BM25F lexical ranking (core.sqlservice.lexical_index) and, when
         enabled, business-concept ranking (core.sqlservice.
         business_concept_index) over the SAME tokenized question — no
         second embedding call, no AI call.
      6. Optional canonical-authority prior (core.sqlservice.
         canonical_scoring) — used ONLY to help order the already-admitted
         frontier, never to admit an object on its own (mirrors core.
         sqlservice.schema_context_retrieval's own canonical-prior
         contract exactly).
      7. Phase 7, Step 3C1 — INDEPENDENT per-lane union (see
         _independent_lane_union): each active real-evidence lane's own top
         `frontier_size` (default 100) candidates are taken in full, then
         deduplicated into one union, capped at `max_admitted_candidates`
         (default 300) — never a shared, lockstep-advancing per-round
         budget across lanes (that prior design capped each lane's
         reachable depth at ~frontier_size/num_active_lanes; see this
         module's own docstring for the real-data Gate A finding that
         caused this correction). An object with zero evidence in every
         lane can never be admitted.
      8. Final ordering by fused RRF score (semantic + lexical +
         business-concept + canonical, identical weight shape to
         core.sqlservice.schema_context_retrieval, UNCHANGED by Step 3C1),
         FQN tie-break.
      9. Build one bounded, immutable FrontierCandidate per admitted key,
         enforcing `max_characters_per_object`; stop admitting further
         candidates (in final fused order) once `max_frontier_characters`
         total would be exceeded — never silently dropping a candidate
         from the MIDDLE of the ordering, only from the tail, and always
         with an explicit warning naming what was cut.

    Deterministic under identical inputs, and under any shuffled ordering
    of the underlying stored documents (index.documents' own load order
    never affects lane ranks/scores/ties, which are computed from cosine
    similarity / BM25F score with FQN tie-breaks throughout).

    Raises:
        ValueError: invalid question or numeric parameter.
        SchemaFrontierValidationError: the embedding provider's response
            was malformed.
    """
    trimmed_question = _validate_question(question)
    frontier_size = _positive_int(frontier_size, "frontier_size")
    max_admitted_candidates = _positive_int(max_admitted_candidates, "max_admitted_candidates")
    max_frontier_characters = _positive_int(max_frontier_characters, "max_frontier_characters")
    max_characters_per_object = _positive_int(max_characters_per_object, "max_characters_per_object")
    for name, value in (
        ("max_aliases_per_object", max_aliases_per_object),
        ("max_columns_per_category", max_columns_per_category),
        ("max_relationships_per_object", max_relationships_per_object),
        ("max_concepts_per_object", max_concepts_per_object),
    ):
        _positive_int(value, name)
    if not isinstance(enable_canonical_prior, bool):
        raise ValueError(f"enable_canonical_prior must be a bool, got {enable_canonical_prior!r}")
    if isinstance(canonical_prior_weight, bool) or not isinstance(canonical_prior_weight, (int, float)) or not (
        0.0 <= canonical_prior_weight <= 1.0
    ):
        raise ValueError(f"canonical_prior_weight must be a number in [0.0, 1.0], got {canonical_prior_weight!r}")
    if not isinstance(enable_business_concept_retrieval, bool):
        raise ValueError(
            f"enable_business_concept_retrieval must be a bool, got {enable_business_concept_retrieval!r}"
        )
    if isinstance(business_concept_weight, bool) or not isinstance(business_concept_weight, (int, float)) or not (
        0.0 <= business_concept_weight <= 1.0
    ):
        raise ValueError(f"business_concept_weight must be a number in [0.0, 1.0], got {business_concept_weight!r}")

    if allowed_tiers is None:
        effective_tiers = DEFAULT_ALLOWED_TIERS
    else:
        effective_tiers = frozenset(allowed_tiers)
        if not effective_tiers or not effective_tiers.issubset(_ALL_TIERS):
            raise ValueError(f"allowed_tiers must be a non-empty subset of {sorted(_ALL_TIERS)}, got {allowed_tiers!r}")

    load_result = load_vector_index(
        build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    if load_result.status != STATUS_OK:
        return _empty_result(load_result.status, build_id, provider, load_result.error)
    index = load_result.index

    # Phase 7, Step 3E — fetched once, here, and reused both for the
    # authority cache identity below and this function's own final
    # schema_fingerprint field (previously fetched a second time at the
    # very end; now a single call). Authority computation makes ZERO
    # embedding/AI calls and does not depend on the question at all, so it
    # is safe (and cheap — cached per build/source, see
    # core.sqlservice.schema_object_authority's own docstring) to run
    # before the one embedding call this function still spends exactly
    # once, unchanged.
    build_row = get_retrieval_build(build_id, source_id, user_id)
    schema_fingerprint = build_row.schema_fingerprint if build_row is not None else None
    authority_result = load_schema_object_authority(index.documents, build_id, source_id, schema_fingerprint)
    authority_by_fqn_lower = authority_result.authorities_by_fqn_lower

    # Phase 7, Step 6E3B1, Part 1 — the FRAMEWORK_BACKED_BUSINESS_VIEW
    # refinement (core.sqlservice.schema_object_authority.classify_
    # framework_backed_eligibility) is computed and persisted at connect
    # time (data.view_profile_service.run_deterministic_view_profiling),
    # never here — this is purely a READ of that already-persisted
    # decision. Only trusted when the profile is COMPLETE, matches
    # THIS build's own schema_fingerprint, and was computed under the
    # current VIEW_PROFILE_CONTRACT_VERSION -- a stale, incomplete, or
    # missing profile never refines anything (fails closed to the
    # original, unmodified UNSAFE_DEPENDENCY verdict below).
    _unsafe_dependency_fqns_needing_refinement = [
        a.object_fqn for a in authority_by_fqn_lower.values()
        if a.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    ]
    refined_class_by_fqn_lower: dict[str, str] = {}
    if _unsafe_dependency_fqns_needing_refinement:
        try:
            from data.view_profile_service import get_view_profiles_by_fqns, get_current_definition_hashes
            from core.sqlservice.view_profiler import classify_profile_state, PROFILE_STATE_COMPLETE
            from core.dictionary.pii_detector import SECRET_POLICY_VERSION
            _profiles = get_view_profiles_by_fqns(source_id, _unsafe_dependency_fqns_needing_refinement)
            _current_hashes = get_current_definition_hashes(source_id, _unsafe_dependency_fqns_needing_refinement)
            for _fqn_lower, _profile in _profiles.items():
                _state = classify_profile_state(
                    _profile, schema_fingerprint=schema_fingerprint,
                    current_definition_hash=_current_hashes.get(_fqn_lower),
                    current_secret_policy_version=SECRET_POLICY_VERSION,
                )
                if _state == PROFILE_STATE_COMPLETE:
                    refined_class_by_fqn_lower[_fqn_lower] = _profile.authority_class
        except Exception:  # noqa: BLE001 -- refinement is additive evidence, never fatal to frontier building
            refined_class_by_fqn_lower = {}

    # Phase 7, Step 3J3 — same "computed once per build/fingerprint, zero
    # embedding/AI cost, cached" shape as authority just above. Never
    # touches retrieval_tier/documents/mappings -- purely an additional
    # admission FILTER (confirmed snapshots) and advisory annotation
    # (uncertain snapshots) applied only within this function.
    snapshot_result = load_snapshot_classification(
        index.documents, build_id, source_id, schema_fingerprint, authority_by_fqn_lower,
    )
    snapshot_by_fqn_lower = snapshot_result.classifications_by_fqn_lower
    confirmed_snapshot_keys = frozenset(
        key for key, c in snapshot_by_fqn_lower.items() if c.classification == SNAPSHOT_CLASS_CONFIRMED
    )

    # Phase 7, Step 3J, Part 5 — same "computed once per build/fingerprint,
    # zero embedding/AI cost, cached" shape as the authority load just
    # above. Returns None (never raises) for a source with no ACTIVE
    # business-knowledge package, or with the feature flag off -- in either
    # case business_knowledge_by_fqn_lower stays {} and every candidate's
    # business_knowledge_mappings field keeps its default empty tuple.
    business_knowledge_by_fqn_lower: Mapping[str, tuple[BusinessKnowledgeMappingEvidence, ...]] = {}
    if BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED:
        business_knowledge_result = load_business_knowledge_evidence(source_id, user_id, schema_fingerprint)
        if business_knowledge_result is not None:
            business_knowledge_by_fqn_lower = business_knowledge_result.mappings_by_object_fqn_lower

    response = provider.embed_batch([trimmed_question])
    query_vector = _validate_single_embedding_response(response, provider.model, provider.dimensions)

    ranked = search_vector_index(index, query_vector, top_k=len(index.documents) or 1)
    tier_filtered = [r for r in ranked if r.retrieval_tier in effective_tiers]

    documents_by_fqn_lower = {d.object_fqn.lower(): d for d in index.documents}
    fqn_by_key = {r.object_fqn.lower(): r.object_fqn for r in tier_filtered}
    semantic_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(tier_filtered)}
    semantic_score_by_key = {r.object_fqn.lower(): r.score for r in tier_filtered}

    lexical_identity = LexicalIndexIdentity(
        build_id=build_id, embedding_provider=provider.provider_id,
        embedding_model=provider.model, embedding_dimensions=provider.dimensions,
    )
    lexical_index = load_lexical_index(index.documents, lexical_identity)
    query_tokens = tokenize(trimmed_question)

    lexical_rank_by_key: dict[str, int] = {}
    lexical_score_by_key: dict[str, float] = {}
    if query_tokens:
        lexical_ranked = search_lexical_index(lexical_index, query_tokens, top_k=lexical_index.document_count or 1)
        lexical_tier_filtered = [r for r in lexical_ranked if r.retrieval_tier in effective_tiers]
        for i, r in enumerate(lexical_tier_filtered):
            key = r.object_fqn.lower()
            lexical_rank_by_key[key] = i + 1
            lexical_score_by_key[key] = r.score
            fqn_by_key.setdefault(key, r.object_fqn)

    business_concept_rank_by_key: dict[str, int] = {}
    business_concept_score_by_key: dict[str, float] = {}
    matched_concepts_by_key: dict[str, tuple] = {}
    if enable_business_concept_retrieval and business_concept_weight and query_tokens:
        business_concept_index = load_business_concept_index(
            build_id, source_id, user_id, index.documents, allowed_tiers=effective_tiers,
        )
        business_concept_ranked = search_business_concept_index(
            business_concept_index, query_tokens, top_k=business_concept_index.lexical_index.document_count or 1,
        )
        for i, r in enumerate(business_concept_ranked):
            key = r.object_fqn.lower()
            if documents_by_fqn_lower.get(key) is None:
                continue
            business_concept_rank_by_key[key] = i + 1
            business_concept_score_by_key[key] = r.score
            matched_concepts_by_key[key] = r.matched_evidence
            fqn_by_key.setdefault(key, r.object_fqn)

    canonical_rank_by_key: dict[str, int] = {}
    canonical_score_by_key: dict[str, float] = {}
    if enable_canonical_prior and canonical_prior_weight > 0.0:
        canonical_identity = CanonicalScoringIdentity(
            build_id=build_id, embedding_provider=provider.provider_id,
            embedding_model=provider.model, embedding_dimensions=provider.dimensions,
        )
        canonical_result = load_canonical_scores(index.documents, canonical_identity)
        canonical_rank_by_key = dict(canonical_result.rank_by_fqn_lower)
        canonical_score_by_key = {
            k: v.total_score for k, v in canonical_result.scores_by_fqn_lower.items()
        }

    admitted_keys, lane_capped = _independent_lane_union(
        {
            LANE_SEMANTIC: semantic_rank_by_key,
            LANE_LEXICAL: lexical_rank_by_key,
            LANE_BUSINESS_CONCEPT: business_concept_rank_by_key,
        },
        fqn_by_key, frontier_size, max_admitted_candidates,
    )

    # Phase 7, Step 5 audit fix — a VERIFIED business-knowledge mapping is
    # owner-confirmed ground truth (data.business_knowledge_mapping_store /
    # business_knowledge_evidence.py's own trust boundary), not a heuristic
    # similarity signal — unlike the three lanes above, it must never depend
    # on whether the object's own name/columns happen to lexically or
    # semantically overlap with the question's literal wording (a table
    # named e.g. "IPLAN_INSTALLMENT_PLAN" can carry a verified mapping to an
    # "installment payment" concept while sharing almost no vocabulary with
    # a plain-English question about it). Force-admit every object with at
    # least one VERIFIED mapping, bounded by the same max_admitted_candidates
    # ceiling as every other admission path — generic over whatever the
    # active business-knowledge package actually contains, never a
    # hardcoded object/table. UNRESOLVED mappings (sub-threshold confidence)
    # are deliberately excluded from this force-admission — they still reach
    # the selector normally, attached below, when the object is independently
    # admitted by one of the three lanes above.
    verified_business_knowledge_keys = sorted(
        key for key, evidence in business_knowledge_by_fqn_lower.items()
        if key in fqn_by_key and any(e.mapping_status == "VERIFIED" for e in evidence)
    )
    for key in verified_business_knowledge_keys:
        if key not in admitted_keys and len(admitted_keys) < max_admitted_candidates:
            admitted_keys.append(key)
    verified_business_knowledge_key_set = frozenset(verified_business_knowledge_keys)

    # Phase 7, Step 6E1 safety hardening (replaces the Step 6E, Part 4
    # rule) — a confirmed relationship hop ALONE is never sufficient
    # authorization to force-admit a Tier 3/4 object. The rule it replaces
    # force-admitted ANY Tier 3/4 neighbor of any already-admitted object
    # purely because a declared FK existed; a live-metadata audit (Phase 7,
    # Step 6E1, Part 1/2) confirmed this reaches dozens of unrelated
    # framework/authentication/permission objects (e.g. every table FK'd to
    # a DotNetNuke-style shared "Users" identity table also used by
    # instructors, document uploaders, campaign/video/survey authorship,
    # and portal role/permission grants — not a students-only table) for
    # any question touching any of their many anchor tables, none of them
    # reviewed or approved for question-answering.
    #
    # A Tier 3/4 object may now be force-admitted ONLY when BOTH hold:
    #   1. It is reachable by exactly one REAL, structurally-confirmed
    #      relationship hop (declared FK or an APPROVED/high-confidence
    #      relationship_service row — the SAME confirmed_relationship_
    #      targets/sources metadata _relationship_neighbor_fqns already
    #      reads for the CONFIRMED_RELATIONSHIPS hint line, never inferred
    #      from column-name matching) from an object already admitted
    #      above.
    #   2. It carries its own VERIFIED business-knowledge mapping — the
    #      SAME owner-confirmed-ground-truth mechanism the verified_
    #      business_knowledge_keys block above already trusts for Tier 1/2
    #      objects. core.sqlservice.business_knowledge_mapper._verify_
    #      proposal already REJECTS AUTHORITY_CLASS_UNSAFE_DEPENDENCY
    #      outright, and every Tier 3/4 table is unconditionally classified
    #      UNSAFE_DEPENDENCY by core.sqlservice.schema_object_authority's
    #      own construction — so condition 2 can only ever be satisfied by
    #      an explicit, manually authored authoritative mapping (a human
    #      decision recorded via data.business_knowledge_mapping_store,
    #      the same path Phase 7 Step 3J2's manual corrections used), NEVER
    #      by AI proposal, model confidence, an object/table name, or a
    #      blanket per-tier exception. No such mapping existed for any
    #      Tier 3/4 object in this source as of the Step 6E1 audit, so this
    #      path currently admits nothing — by design (fail closed until a
    #      human explicitly approves a specific object + column set).
    # Bounded by the same max_admitted_candidates ceiling; only ever looks
    # one hop out from an object ALREADY admitted through a real signal
    # above (never chains through a just-forced neighbor), so this can
    # never cascade into admitting a whole system schema. When the VERIFIED
    # mapping also names specific approved columns, only those columns are
    # rendered for this candidate (see governance_restricted_columns_by_key
    # below) — a mapping with no columns recorded is treated as approving
    # the object's identity/join columns only (nothing is exposed beyond
    # what render_frontier_candidate already always shows: PRIMARY_KEY/
    # FOREIGN_KEYS/CONFIRMED_RELATIONSHIPS), never every column by default.
    relationship_forced_keys: list[str] = []
    governance_restricted_columns_by_key: dict[str, frozenset[str]] = {}
    for anchor_key in sorted(admitted_keys):
        anchor_doc = documents_by_fqn_lower.get(anchor_key)
        if anchor_doc is None:
            continue
        for neighbor_fqn in _relationship_neighbor_fqns(anchor_doc, limit=max_relationships_per_object):
            neighbor_key = neighbor_fqn.lower()
            neighbor_doc = documents_by_fqn_lower.get(neighbor_key)
            if neighbor_doc is None or neighbor_doc.retrieval_tier in (1, 2):
                continue  # tier-1/2 neighbors already compete for admission normally
            if neighbor_key in admitted_keys or neighbor_key in relationship_forced_keys:
                continue
            neighbor_mappings = business_knowledge_by_fqn_lower.get(neighbor_key, ())
            verified_mappings = [m for m in neighbor_mappings if m.mapping_status == "VERIFIED"]
            if not verified_mappings:
                continue  # no owner-confirmed governance record for this object -- fail closed
            if len(admitted_keys) + len(relationship_forced_keys) >= max_admitted_candidates:
                continue
            relationship_forced_keys.append(neighbor_key)
            fqn_by_key.setdefault(neighbor_key, neighbor_doc.object_fqn)
            # Always recorded, even when empty -- a VERIFIED mapping that
            # names no specific columns approves the object's structural
            # join/identity columns ONLY (never every column by default;
            # see this block's own comment above), so an empty set here
            # must still filter every selectable attribute column down to
            # nothing, exactly like an explicit column list would.
            governance_restricted_columns_by_key[neighbor_key] = frozenset(
                str(c).strip().lower() for m in verified_mappings for c in (m.columns or ())
            )
    admitted_keys.extend(relationship_forced_keys)
    relationship_forced_key_set = frozenset(relationship_forced_keys)

    # Phase 7, Step 3J3, Part 3 — prevent ONLY high-confidence CONFIRMED
    # snapshots from ever becoming a candidate. Applied here, after normal
    # lane admission and before ranking/rendering -- the object was never
    # excluded from the underlying index/documents/mappings (see this
    # module's own snapshot_classifier import docstring), only from THIS
    # frontier's own admitted set. Every suppression is reported by FQN,
    # canonical sibling, reason, and confidence -- never silent.
    suppressed_snapshot_keys = [k for k in admitted_keys if k in confirmed_snapshot_keys]
    if suppressed_snapshot_keys:
        admitted_keys = [k for k in admitted_keys if k not in confirmed_snapshot_keys]

    # Uncertain (below-confirmation-threshold) snapshot candidates are
    # never excluded -- only demoted in ranking (a bounded, deterministic
    # multiplicative penalty) and annotated on their own FrontierCandidate
    # (see business_knowledge_mappings-style merge below) so the selector
    # itself can see and weigh the same evidence a human would.
    def _snapshot_penalty(key: str) -> float:
        classification = snapshot_by_fqn_lower.get(key)
        if classification is not None and classification.classification == SNAPSHOT_CLASS_UNCERTAIN:
            return _UNCERTAIN_SNAPSHOT_RANK_PENALTY
        return 1.0

    final_order = sorted(
        admitted_keys,
        key=lambda k: (
            # Owner-verified business-knowledge evidence and a real
            # confirmed-relationship force-admit both outrank heuristic
            # fused similarity -- otherwise a force-admitted object with no
            # semantic/lexical/business-concept rank at all (fused score
            # 0.0) would sort to the very bottom and be the first thing the
            # character-budget trim below drops, defeating the point of
            # force-admitting it in the first place.
            0 if (k in verified_business_knowledge_key_set or k in relationship_forced_key_set) else 1,
            -_fuse(
                semantic_rank_by_key.get(k), lexical_rank_by_key.get(k), business_concept_rank_by_key.get(k),
                business_concept_weight, canonical_rank_by_key.get(k), canonical_prior_weight,
            ) * _snapshot_penalty(k),
            fqn_by_key[k],
        ),
    )

    warnings: list[str] = []
    if not admitted_keys and not suppressed_snapshot_keys:
        warnings.append("no documents matched the allowed retrieval tiers for this question")
    if lane_capped:
        warnings.append(
            f"max_admitted_candidates={max_admitted_candidates} was reached while a lane still had "
            "unexamined top-ranked candidates left -- the union, not any lane's own top-N, was the "
            "limiting factor for this question"
        )
    for key in suppressed_snapshot_keys:
        classification = snapshot_by_fqn_lower[key]
        warnings.append(
            f"suppressed confirmed snapshot candidate {classification.object_fqn!r}: "
            f"canonical_sibling={classification.canonical_sibling_fqn!r} "
            f"reason={classification.reason} confidence={classification.confidence:.2f}"
        )
    if relationship_forced_keys:
        warnings.append(
            "force-admitted "
            f"{len(relationship_forced_keys)} tier-3/4 object(s) via a confirmed relationship hop AND "
            f"an owner-confirmed VERIFIED business-knowledge mapping: "
            f"{', '.join(fqn_by_key[k] for k in sorted(relationship_forced_keys))}"
        )

    # Phase 7, Step 3F, Part 2 — collapse only ALREADY-PROVEN canonical/
    # equivalent object families down to one selectable representative
    # each. Runs on the already fused-ranked admission list, before any
    # candidate is rendered — never changes lane admission, ranking
    # weights, or frontier_size; it only removes objects the ranking
    # already admitted that core.sqlservice.schema_object_authority has
    # already structurally proven redundant.
    final_order, authority_collapsed_members_by_key = _collapse_authority_family_representatives(
        final_order, authority_by_fqn_lower, fqn_by_key,
    )
    if authority_collapsed_members_by_key:
        total_collapsed = sum(len(v) for v in authority_collapsed_members_by_key.values())
        warnings.append(
            f"authority-family collapse: {total_collapsed} proven canonical/equivalent alternative(s) "
            f"merged into {len(authority_collapsed_members_by_key)} representative(s)"
        )

    query_tokens_set = frozenset(query_tokens)
    candidates: list[FrontierCandidate] = []
    used_characters = 0
    truncated = False
    for rank, key in enumerate(final_order, start=1):
        doc = documents_by_fqn_lower[key]
        metadata = doc.metadata
        authority = authority_by_fqn_lower.get(key)

        # Phase 7, Step 6E3B1, Part 1 — apply the persisted, build-fresh
        # refinement looked up above. `effective_*` is what every
        # downstream gate (schema_selection_service._validate_authority_
        # facts, schema_selection_verifier._check_forbidden_authority) and
        # this candidate's own rendered AUTHORITY line actually see;
        # `authority.unsafe_dependency_fqns` itself, and the ALWAYS-
        # populated authority_unsafe_dependency_evidence_fqns field below,
        # are never mutated — the original evidence is preserved
        # regardless of whether the refinement applies.
        effective_authority_class = authority.authority_class if authority else "UNKNOWN"
        effective_unsafe_dependency_fqns = authority.unsafe_dependency_fqns if authority else ()
        if effective_authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY:
            _refined = refined_class_by_fqn_lower.get(key)
            if _refined == AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW:
                effective_authority_class = _refined
                effective_unsafe_dependency_fqns = ()
        lanes = tuple(
            name for name, ranks in (
                (LANE_SEMANTIC, semantic_rank_by_key), (LANE_LEXICAL, lexical_rank_by_key),
                (LANE_BUSINESS_CONCEPT, business_concept_rank_by_key),
            ) if key in ranks
        )
        # Phase 7, Step 6E1 — a Tier 3/4 object force-admitted above via a
        # VERIFIED business-knowledge mapping that names specific columns
        # is rendered with ONLY those columns as selectable attributes;
        # every other candidate (including a Tier 3/4 object whose mapping
        # named no columns) is unaffected. Never touches primary_key_
        # columns/foreign_key_summary/local_foreign_key_columns -- those
        # are structural join identity, not selectable business content.
        approved_columns = governance_restricted_columns_by_key.get(key)
        measure_columns_raw = metadata.get("measure_columns") or ()
        date_columns_raw = metadata.get("date_columns") or ()
        status_columns_raw = metadata.get("status_columns") or ()
        dimension_columns_raw = metadata.get("column_names") or ()
        if approved_columns is not None:
            measure_columns_raw = tuple(c for c in measure_columns_raw if str(c).strip().lower() in approved_columns)
            date_columns_raw = tuple(c for c in date_columns_raw if str(c).strip().lower() in approved_columns)
            status_columns_raw = tuple(c for c in status_columns_raw if str(c).strip().lower() in approved_columns)
            dimension_columns_raw = tuple(
                c for c in dimension_columns_raw if str(c).strip().lower() in approved_columns
            )
        candidate = FrontierCandidate(
            object_fqn=doc.object_fqn, object_type=doc.object_type, retrieval_tier=doc.retrieval_tier,
            business_name=metadata.get("business_name") or None,
            business_purpose=metadata.get("description") or None,
            domain=metadata.get("domain") or None,
            aliases=tuple((metadata.get("synonyms") or ())[:max_aliases_per_object]),
            # Key/semantic-evidence column categories are deliberately NEVER
            # truncated at CONSTRUCTION time (Step 3C1) -- only dimension_
            # columns is bounded here by max_columns_per_category. All four
            # of measure_/date_/status_/dimension_columns are now ORDERED
            # by question relevance first, alphabetical only as the final
            # tie-break (Step 3F, Part 3, requirement 6) -- this is what
            # lets _fit_to_character_budget's later progressive trim always
            # discard the globally least-relevant column first, rather than
            # an entire category at once regardless of relevance.
            primary_key_columns=tuple(metadata.get("primary_key_columns") or ()),
            foreign_key_summary=tuple((metadata.get("foreign_key_targets") or ())[:max_columns_per_category]),
            local_foreign_key_columns=_local_foreign_key_columns(doc),
            measure_columns=_order_columns_by_relevance(measure_columns_raw, query_tokens_set),
            date_columns=_order_columns_by_relevance(date_columns_raw, query_tokens_set),
            status_columns=_order_columns_by_relevance(status_columns_raw, query_tokens_set),
            dimension_columns=_order_columns_by_relevance(
                dimension_columns_raw, query_tokens_set,
            )[:max_columns_per_category],
            confirmed_relationship_neighbors=_relationship_neighbor_fqns(doc, limit=max_relationships_per_object),
            confirmed_relationship_column_pairs=_confirmed_relationship_column_pairs(doc),
            semantic_rank=semantic_rank_by_key.get(key), semantic_score=semantic_score_by_key.get(key),
            lexical_rank=lexical_rank_by_key.get(key), lexical_score=lexical_score_by_key.get(key),
            business_concept_rank=business_concept_rank_by_key.get(key),
            business_concept_score=business_concept_score_by_key.get(key),
            canonical_rank=canonical_rank_by_key.get(key), canonical_total_score=canonical_score_by_key.get(key),
            fused_rank=rank,
            fused_score=_fuse(
                semantic_rank_by_key.get(key), lexical_rank_by_key.get(key), business_concept_rank_by_key.get(key),
                business_concept_weight, canonical_rank_by_key.get(key), canonical_prior_weight,
            ),
            admitted_via_lanes=lanes,
            matched_business_concepts=tuple((matched_concepts_by_key.get(key) or ())[:max_concepts_per_object]),
            authority_class=effective_authority_class,
            authority_canonical_table_fqns=authority.canonical_table_fqns if authority else (),
            authority_is_declared_preferred_view=bool(authority and authority.preferred_view_for_entities),
            authority_restriction_status=authority.restriction_status if authority else "UNKNOWN",
            authority_restrictions=authority.restrictions if authority else (),
            authority_grain_class=authority.grain_class if authority else "UNKNOWN",
            authority_aggregation_status=authority.aggregation_status if authority else "UNKNOWN",
            authority_row_preservation_status=authority.row_preservation_status if authority else "UNKNOWN",
            authority_safe_dependency_count=(
                len(authority.dependency_fqns) - len(authority.unsafe_dependency_fqns) if authority else 0
            ),
            authority_unsafe_dependency_fqns=effective_unsafe_dependency_fqns,
            authority_unsafe_dependency_evidence_fqns=authority.unsafe_dependency_fqns if authority else (),
            authority_evidence=authority.evidence if authority else (),
            authority_preferred_view_for_entities=authority.preferred_view_for_entities if authority else (),
            authority_collapsed_member_fqns=authority_collapsed_members_by_key.get(key, ())[
                :_MAX_COLLAPSED_MEMBERS_PER_REPRESENTATIVE
            ],
            business_knowledge_mappings=tuple(
                (business_knowledge_by_fqn_lower.get(key) or ())[:max_concepts_per_object]
            ),
            snapshot_uncertain=(
                snapshot_by_fqn_lower.get(key) is not None
                and snapshot_by_fqn_lower[key].classification == SNAPSHOT_CLASS_UNCERTAIN
            ),
            snapshot_confidence=(snapshot_by_fqn_lower[key].confidence if key in snapshot_by_fqn_lower else 0.0),
            snapshot_canonical_sibling_fqn=(
                snapshot_by_fqn_lower[key].canonical_sibling_fqn if key in snapshot_by_fqn_lower else None
            ),
            snapshot_reason=(snapshot_by_fqn_lower[key].reason if key in snapshot_by_fqn_lower else None),
        )
        candidate, column_summary_incomplete = _fit_to_character_budget(
            candidate, max_characters_per_object, query_tokens_set,
        )
        if column_summary_incomplete:
            warnings.append(
                f"{candidate.object_fqn}: column summary incomplete -- a question-relevant column had to be "
                "trimmed to fit the per-object character budget"
            )
        text_length = len(render_frontier_candidate(candidate))
        addition = text_length + (len(_CONTEXT_SEPARATOR) if candidates else 0)
        if used_characters + addition > max_frontier_characters:
            truncated = True
            warnings.append(
                f"frontier truncated at {len(candidates)} of {len(final_order)} admitted candidates: "
                f"adding {candidate.object_fqn!r} would exceed the {max_frontier_characters}-character total budget"
            )
            break
        candidates.append(candidate)
        used_characters += addition

    return SchemaFrontierResult(
        status=STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider=provider.provider_id, embedding_model=provider.model,
        embedding_dimensions=provider.dimensions, candidates=tuple(candidates),
        total_characters=used_characters, truncated=truncated, warnings=tuple(warnings), error=None,
        admitted_before_authority_collapse=len(admitted_keys),
    )
