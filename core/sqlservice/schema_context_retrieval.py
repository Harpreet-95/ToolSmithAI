"""
Semantic schema retrieval for a natural-language question — SQL rebuild
Phase 2, Step 4.

Thin orchestration layer over three existing building blocks — it does not
duplicate any of their logic:

  1. core.sqlservice.embedding_providers.EmbeddingProvider
     — embeds exactly one question via embed_batch([question]).
  2. core.sqlservice.vector_index.load_vector_index() /
     search_vector_index()
     — the ownership/READY/completeness gate, the cached immutable index,
       and exact cosine search — no SQLite access happens once the index
       is loaded.
  3. core.sqlservice.retrieval_store.get_retrieval_build()
     — the build's schema_fingerprint, for this result's identity fields.

This module's own job is exactly what's between those: validate the
question and the provider's single-item response, apply the tier policy,
expand one hop of *confirmed* relationships from each semantic match's own
metadata (never inventing a new relationship shape), and pack the result
into a deterministic, character-budgeted context package.

Scope: never calls a real embedding provider itself (injected by the
caller), never generates SQL, never connects to a live SQL Server, never
integrates with the connection-time coordinator, and never logs or
persists the question text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from core.sqlservice.business_concept_index import (
    load_business_concept_index,
    search_business_concept_index,
)
from core.sqlservice.canonical_scoring import (
    CanonicalScoringIdentity,
    CanonicalScoringResult,
    load_canonical_scores,
)
from core.sqlservice.context_packing import (
    SELECTION_REASON_DIRECT_RELATIONSHIP,
    SELECTION_REASON_EXAMPLE_DERIVED,
    SELECTION_REASON_HYBRID_ANCHOR,
    SELECTION_REASON_POOL_EXEMPT_NEIGHBOR,
    SELECTION_REASON_RELATIONSHIP_BRIDGE,
    PackCandidate,
    pack_candidates,
)
from core.sqlservice.embedding_providers import EmbeddingProvider, ProviderEmbeddingBatchResponse
from core.sqlservice.lexical_index import (
    LexicalIndexIdentity,
    load_lexical_index,
    search_lexical_index,
    tokenize,
)
from core.sqlservice.relationship_graph import (
    MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH,
    build_relationship_graph,
    find_shortest_relationship_path,
)
from core.sqlservice.retrieval_store import get_retrieval_build
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
    "SELECTION_REASON_SEMANTIC",
    "SELECTION_REASON_RELATIONSHIP",
    "SELECTION_REASON_HYBRID_ANCHOR",
    "SELECTION_REASON_RELATIONSHIP_BRIDGE",
    "SELECTION_REASON_DIRECT_RELATIONSHIP",
    "SELECTION_REASON_POOL_EXEMPT_NEIGHBOR",
    "RANKING_MODE_HYBRID",
    "RANKING_MODE_VECTOR",
    "DEFAULT_RANKING_MODE",
    "RRF_K",
    "DEFAULT_TOP_K",
    "DEFAULT_RELATIONSHIP_LIMIT",
    "DEFAULT_MAX_CONTEXT_CHARACTERS",
    "DEFAULT_ALLOWED_TIERS",
    "DEFAULT_CANDIDATE_POOL_SIZE",
    "DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH",
    "DEFAULT_MAX_PROJECTED_COLUMNS",
    "DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR",
    "DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL",
    "DEFAULT_ENABLE_CANONICAL_PRIOR",
    "DEFAULT_CANONICAL_PRIOR_WEIGHT",
    "DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL",
    "DEFAULT_BUSINESS_CONCEPT_WEIGHT",
    "MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH",
    "SchemaRetrievalError",
    "SchemaRetrievalValidationError",
    "ContextDocument",
    "SchemaContextResult",
    "retrieve_schema_context",
    "EXAMPLE_RETRIEVAL_NOT_ATTEMPTED",
    "SchemaContextWithExamplesResult",
    "retrieve_schema_context_and_examples",
]

# ── Defaults (all caller-overridable — see retrieve_schema_context) ──────

DEFAULT_TOP_K = 8
DEFAULT_RELATIONSHIP_LIMIT = 5
DEFAULT_MAX_CONTEXT_CHARACTERS = 8000
DEFAULT_ALLOWED_TIERS = frozenset({1, 2})  # normal-business default: Tier 1+2 only
_ALL_TIERS = frozenset({1, 2, 3, 4})
_MAX_QUESTION_CHARACTERS = 2000

# Phase 5, Step 5 — candidate pool / relationship-path / packing defaults.
# All four are caller-overridable, bounded safeguards (see
# retrieve_schema_context's own validation) — never used by
# RANKING_MODE_VECTOR, which keeps the exact pre-Step-5 simple behavior.
DEFAULT_CANDIDATE_POOL_SIZE = 40         # must be >= top_k — "larger than the final requested object count"
DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH = 2  # edges; hard-capped at MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH (3)
DEFAULT_MAX_PROJECTED_COLUMNS = 12        # per compact projection's MATCHED_COLUMNS/STATUS_COLUMNS/etc. lines

# Phase 5, Step 7, Part 2 — pool-exempt confirmed neighbor defaults. Both
# caller-overridable, non-negative (0 disables the mechanism entirely) —
# see retrieve_schema_context's own validation and the pool-exempt
# selection pass for the full eligibility/ranking/limit contract.
DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR = 3
DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL = 8

# Phase 5, Step 8 — canonical-object scoring as a bounded relevance prior.
# Applied ONLY when re-ranking the ALREADY relevance-selected candidate
# pool to choose which top_k become anchors (requirement 2/6) — pool
# MEMBERSHIP itself (`pool_keys` below) is computed from semantic+lexical
# fusion alone, unchanged, so an object with zero query relevance can never
# be pulled in "solely because of its canonical score" (requirement 3): it
# must already have earned a place in the pool on relevance terms alone.
#
# DEFAULT_CANONICAL_PRIOR_WEIGHT < 1.0 is deliberate: reused verbatim in
# the SAME bounded RRF term shape (weight / (RRF_K + canonical_rank)) every
# other ranking signal in this module uses (requirement 4 — "principled
# normalized/rank-based combination... never arbitrary unbounded raw
# scores") — a fractional weight means canonical's own maximum possible
# contribution (rank 1: weight/RRF_K) is strictly smaller than a single
# genuine semantic or lexical rank's own maximum contribution (1/RRF_K),
# so canonical alone can nudge but never outweigh real query relevance.
DEFAULT_ENABLE_CANONICAL_PRIOR = True
DEFAULT_CANONICAL_PRIOR_WEIGHT = 0.5

# Phase 7, Step 3B, Part 2 — automatic business-concept retrieval as a
# THIRD fused ranking term, alongside semantic and lexical (never merely a
# prior over an already-fixed pool the way canonical scoring is — this term
# participates in POOL MEMBERSHIP itself, exactly like lexical, because the
# whole point is to let a technically-named object with real business
# evidence (e.g. an entity/domain assignment or a resolved semantic entity
# contract) enter the candidate pool even when its literal name/columns
# score poorly on both semantic and lexical grounds). Weight defaults to
# full parity (1.0) with the semantic/lexical terms — unlike canonical
# scoring, this signal IS genuine, query-driven relevance (an object only
# ever receives a rank here when it has actual matched evidence for THIS
# question's own terms), not a query-independent structural prior, so there
# is no principled reason to subordinate it the way DEFAULT_CANONICAL_
# PRIOR_WEIGHT deliberately does.
DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL = True
DEFAULT_BUSINESS_CONCEPT_WEIGHT = 1.0

SELECTION_REASON_SEMANTIC = "SEMANTIC_MATCH"
SELECTION_REASON_RELATIONSHIP = "RELATIONSHIP_EXPANSION"

# Phase 5, Step 4 — deterministic hybrid semantic + lexical ranking.
# RANKING_MODE_HYBRID (the new default) fuses cosine-similarity rank with
# BM25F lexical rank via reciprocal-rank fusion (see _fuse_ranks below).
# RANKING_MODE_VECTOR reproduces the exact pre-Step-4 pure-vector behavior —
# preserved as a configurable, testable comparison mode, never removed.
RANKING_MODE_HYBRID = "hybrid"
RANKING_MODE_VECTOR = "vector"
DEFAULT_RANKING_MODE = RANKING_MODE_HYBRID
_ALLOWED_RANKING_MODES = frozenset({RANKING_MODE_HYBRID, RANKING_MODE_VECTOR})

# Reciprocal Rank Fusion constant — the standard value from the original RRF
# paper (Cormack, Clarke & Buettcher 2009), not a tuned/arbitrary number.
# fused_score(doc) = sum over each ranking the doc appears in of
# 1/(RRF_K + rank_in_that_ranking). A doc absent from a ranking (e.g. zero
# lexical-field overlap) simply omits that term — never a fabricated
# worst-case rank — so a purely-semantic match remains fully discoverable
# on its semantic rank alone.
RRF_K = 60

_CONTEXT_SEPARATOR = "\n\n"


class SchemaRetrievalError(Exception):
    """Base for exceptions this module raises itself. EmbeddingConfigurationError/
    EmbeddingProviderError from core.sqlservice.embedding_providers are NOT
    subclasses of this — they propagate unchanged, exactly as in
    core.sqlservice.embedding_coordinator."""


class SchemaRetrievalValidationError(SchemaRetrievalError):
    """The provider's single-item embedding response failed validation
    (wrong count, wrong index, model mismatch, wrong dimensions, or a
    non-finite/zero vector) — the same category as core.sqlservice.
    embedding_coordinator.EmbeddingValidationError, specialized to exactly
    one input/output pair."""


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContextDocument:
    """One document selected into a SchemaContextResult. `score` is the
    cosine similarity for a semantic match, or None for a document added
    only through relationship expansion (it was never scored against the
    question). `metadata` is the same deeply-immutable mapping the index
    already carries — never re-wrapped.

    Phase 5, Step 4 observability additions (all default-safe — every
    existing caller/test that never mentions them keeps working):
    semantic_rank/lexical_rank are this document's 1-indexed position
    within the tier-filtered semantic/lexical ranking respectively (None
    when the document never appeared in that ranking at all — e.g. zero
    lexical-field overlap, or RANKING_MODE_VECTOR never computes a lexical
    rank). lexical_score is this document's raw BM25F score (None under
    the same conditions as lexical_rank). fused_score is the reciprocal-
    rank-fusion score that actually drove selection/ordering under hybrid
    mode (equal to the semantic-only RRF term under RANKING_MODE_VECTOR,
    since no lexical term exists to add); None for a relationship-expansion
    document, which was never ranked at all. matched_lexical_fields names
    which structured fields (e.g. "SYNONYMS", "COLUMN_NAMES") contributed a
    nonzero lexical score — never the raw matched document text/embeddings,
    so this is safe to surface without exposing internal document
    contents beyond what document_text/metadata already carry.

    Phase 5, Step 5 addition: document_text/document_hash ALWAYS carry the
    original, complete document's own text/hash for identity/auditing,
    regardless of whether the packer actually used a compact structured
    projection instead. `used_compact_text` (default False, so every prior
    caller/test keeps working unchanged) marks that separately: True means
    this document's contribution to the enclosing SchemaContextResult's
    context_text is a compact projection, not document_text verbatim —
    never a mix, never a partial/split document either way.

    Phase 5, Step 7, Part 2 additions (both default None, so every prior
    caller/test keeps working unchanged): `source_anchor_fqn` and
    `pool_exempt_reason` are populated ONLY when selection_reason ==
    SELECTION_REASON_POOL_EXEMPT_NEIGHBOR — the confirmed hybrid anchor
    this neighbor was admitted through, and a short deterministic
    explanation (provenance kind, and whether it also touches other
    anchors) of why it was let in despite falling outside the fused
    candidate pool. None for every other selection_reason.

    Phase 5, Step 8 additions (all default None — full immutable
    observability into core.sqlservice.canonical_scoring's per-build
    assessment, requirement 7). Populated for every packed document that
    has a canonical score (i.e. was part of the fused candidate pool for
    this question — bridges/pool-exempt neighbors from OUTSIDE the pool
    still get their own canonical score/family info here for observability,
    even though canonical scoring never influenced whether THEY were
    selected, only anchor selection did — see `canonical_changed_anchor_rank`):
    `canonical_total_score`/`canonical_components` are
    core.sqlservice.canonical_scoring.CanonicalObjectScore's own
    total_score/components verbatim. `canonical_rank` is this object's
    1-indexed GLOBAL canonical rank across the whole build (not
    pool-scoped). `canonical_family_key`/`canonical_family_size`/
    `canonical_family_rank` describe its automatically detected comparable
    family. `canonical_competing_fqns` lists OTHER candidate-pool members
    sharing its family (empty tuple when none). `canonical_changed_anchor_rank`
    is True/False ONLY for selection_reason == SELECTION_REASON_HYBRID_ANCHOR
    (whether adding the canonical prior actually changed this object's
    position within the candidate pool relative to relevance-only
    ordering) — None for every other selection_reason, since canonical
    scoring never affects bridge/direct/pool-exempt selection at all
    (requirement 6).

    Phase 5, Step 9 additions (both default None): `example_id`/
    `example_derived_reason` are populated ONLY when selection_reason ==
    SELECTION_REASON_EXAMPLE_DERIVED — the id of the retrieved trusted
    core.sqlservice.sql_example_model.SqlExample that proposed this object
    as a schema candidate, and a short deterministic explanation. See
    retrieve_schema_context's `example_proposed_objects` parameter."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]
    score: float | None
    selection_reason: str  # SELECTION_REASON_SEMANTIC | SELECTION_REASON_RELATIONSHIP | SELECTION_REASON_HYBRID_ANCHOR | SELECTION_REASON_RELATIONSHIP_BRIDGE | SELECTION_REASON_DIRECT_RELATIONSHIP | SELECTION_REASON_POOL_EXEMPT_NEIGHBOR
    semantic_rank: int | None = None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    fused_score: float | None = None
    matched_lexical_fields: tuple[str, ...] = ()
    used_compact_text: bool = False
    source_anchor_fqn: str | None = None
    pool_exempt_reason: str | None = None
    canonical_total_score: float | None = None
    canonical_components: Mapping[str, float] | None = None
    canonical_rank: int | None = None
    canonical_family_key: str | None = None
    canonical_family_size: int | None = None
    canonical_family_rank: int | None = None
    canonical_competing_fqns: tuple[str, ...] | None = None
    canonical_changed_anchor_rank: bool | None = None
    example_id: int | None = None
    example_derived_reason: str | None = None

    # Phase 7, Step 3B, Part 2 additions (all default-safe: None/() so
    # every prior caller/test keeps working unchanged). business_concept_
    # rank/score mirror lexical_rank/lexical_score exactly, but from
    # core.sqlservice.business_concept_index's own BM25F ranking over
    # trusted, persisted business evidence (OBJECT_CORE/VIEW_ENRICHED
    # aliases, entity/domain assignments, resolved semantic entity
    # contracts) instead of schema-structural fields — None whenever this
    # object matched no such evidence at all for this question (the
    # "additional lane, never an override" requirement: absence here never
    # penalizes a document, it simply contributes no fused-score term).
    # matched_business_concepts is the bounded, deduped, provenance-carrying
    # evidence (core.sqlservice.business_concept_index.MatchedConceptEvidence)
    # that produced this document's business-concept score — never the raw
    # index internals, so this is safe to surface for observability exactly
    # like matched_lexical_fields already is.
    business_concept_rank: int | None = None
    business_concept_score: float | None = None
    matched_business_concepts: tuple = ()


@dataclass(frozen=True)
class SchemaContextResult:
    """
    Result of one retrieve_schema_context() call.

    status: STATUS_OK | STATUS_UNOWNED | STATUS_BUILD_NOT_FOUND |
        STATUS_BUILD_NOT_READY | STATUS_INCOMPLETE | STATUS_INVALID —
        propagated unchanged from load_vector_index() whenever the index
        itself couldn't be loaded (see `error` for detail); STATUS_OK
        whenever `documents`/`context_text` are meaningful.
    schema_fingerprint: the build's structural fingerprint, for the
        text-to-SQL step to detect a schema that has drifted since this
        context was built. None only when status != STATUS_OK.

    Deliberately does NOT carry a query_vector field (Phase 5, Step 9A
    hardening) — a question embedding must never be exposed through a
    public result that a route/adapter could serialize (e.g. a generic
    dataclasses.asdict() over any result reachable from an API response).
    A caller wanting dynamic few-shot SQL examples reuses the SAME query
    vector this call's one provider.embed_batch() call produced through
    retrieve_schema_context_and_examples() below instead — an internal-
    only flow: the vector is created once, used for both schema search and
    example search, and never leaves this module as data on a public
    dataclass. See _retrieve_schema_context_with_vector (private).
    """

    status: str
    build_id: int
    schema_fingerprint: str | None
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    documents: tuple[ContextDocument, ...]
    context_text: str
    total_characters: int
    truncated: bool
    warnings: tuple[str, ...]
    error: str | None
    ranking_mode: str = DEFAULT_RANKING_MODE  # RANKING_MODE_HYBRID | RANKING_MODE_VECTOR
    # Phase 7, Step 3A, Part 2, requirement 8. Default False — every prior
    # caller/test that never mentions this field keeps working unchanged.
    # True only under RANKING_MODE_HYBRID, and only when a connected anchor
    # or a required confirmed-relationship bridge could not be packed at
    # all (not even compactly) within max_context_characters — see the
    # distinctly-prefixed "INCOMPLETE_CONTEXT:" warning this same call adds
    # to `warnings` when this is True, naming exactly which object(s) were
    # lost. Never silently presented as a sufficient context: a caller that
    # cares can check this flag directly instead of pattern-matching
    # `warnings` text.
    essential_context_incomplete: bool = False


def _empty_result(status: str, build_id: int, provider: EmbeddingProvider, error: str | None) -> SchemaContextResult:
    return SchemaContextResult(
        status=status,
        build_id=build_id,
        schema_fingerprint=None,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        documents=(),
        context_text="",
        total_characters=0,
        truncated=False,
        warnings=(),
        error=error,
    )


# ── Question validation (never logged, never persisted) ───────────────────


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


# ── Provider response validation (single item, index 0) ───────────────────


def _validate_single_embedding_response(
    response: ProviderEmbeddingBatchResponse, expected_model: str, expected_dimensions: int,
) -> tuple[float, ...]:
    items = response.items
    if len(items) != 1:
        raise SchemaRetrievalValidationError(
            f"provider returned {len(items)} embeddings for a single-question request, expected exactly 1"
        )

    item = items[0]
    if not isinstance(item.index, int) or isinstance(item.index, bool) or item.index != 0:
        raise SchemaRetrievalValidationError(f"provider returned index {item.index!r}, expected exactly 0")

    if response.model != expected_model:
        raise SchemaRetrievalValidationError(
            f"provider response model {response.model!r} does not match the expected model {expected_model!r}"
        )

    vector = item.vector
    try:
        length = len(vector)
    except TypeError as exc:
        raise SchemaRetrievalValidationError("provider returned a non-sequence vector") from exc
    if length != expected_dimensions:
        raise SchemaRetrievalValidationError(
            f"provider returned {length} dimensions, expected {expected_dimensions}"
        )

    floats: list[float] = []
    for i, raw in enumerate(vector):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise SchemaRetrievalValidationError(f"vector element {i} is not a number: {raw!r}") from exc
        if math.isnan(value) or math.isinf(value):
            raise SchemaRetrievalValidationError(f"vector element {i} is not finite: {value!r}")
        floats.append(value)

    norm = math.sqrt(sum(f * f for f in floats))
    if norm == 0.0:
        raise SchemaRetrievalValidationError("provider returned a zero vector, which cannot be used as a query")

    return tuple(floats)


# ── Relationship expansion (confirmed metadata only, one hop) ─────────────


def _relationship_candidate_fqns(doc: IndexedSchemaDocument) -> list[str]:
    """
    Every candidate object_fqn one confirmed relationship hop away from
    `doc`, reading the exact metadata keys core.sqlservice.
    retrieval_documents.build_retrieval_documents() already writes —
    "confirmed_relationship_targets" (outbound) and
    "confirmed_relationship_sources" (inbound). Never
    "inferred_relationship_*" — only confirmed edges are ever expanded.

    Each entry is a "schema.table.column" descriptor (see
    core/sqlservice/retrieval_documents.py's _fk_target/_qualify); the
    object identity is everything before the last dot — schema/table
    names in this project never contain a literal dot themselves, so
    rpartition(".") on the last segment is exact, not a heuristic.
    """
    targets = doc.metadata.get("confirmed_relationship_targets") or ()
    sources = doc.metadata.get("confirmed_relationship_sources") or ()
    candidates: list[str] = []
    for descriptor in (*targets, *sources):
        object_fqn, sep, _column = descriptor.rpartition(".")
        if sep and object_fqn:
            candidates.append(object_fqn)
    return candidates


# ── Pool-exempt confirmed neighbors (Step 7, Part 2) ───────────────────────
#
# For every hybrid anchor, a confirmed direct neighbor OUTSIDE the fused
# candidate pool may still be structurally essential (a bridge/join target
# the pool's relevance-only cutoff happened to exclude). Eligibility is
# already fully enforced by construction, before this code ever runs: the
# candidates considered are exactly `graph.adjacency.get(anchor_key, ())`,
# where `graph` (core.sqlservice.relationship_graph.build_relationship_graph)
# was built ONLY from tier-allowed documents (effective_tiers — excludes
# Tier 3/4) and ONLY confirmed edges (never inferred/pending/rejected, by
# that module's own construction — see its docstring). Cross-database
# objects cannot appear at all (one build/index is always scoped to one
# source). An unknown/dangling reference is silently absent from
# `graph.adjacency` (relationship_graph's own contract), so it can never be
# proposed as a candidate here either.

_RELATIONSHIP_KIND_PRIORITY: Mapping[str, int] = {
    "PHYSICAL_FK": 0,
    "CATALOG_VIEW_DEPENDENCY": 1,
    "OTHER_CONFIRMED": 2,
}


def _confirmed_edge_kind(anchor_doc: IndexedSchemaDocument, neighbor_fqn_lower: str) -> str:
    """
    The best (highest-priority) provenance kind of any confirmed edge on
    `anchor_doc` pointing at `neighbor_fqn_lower`, reading the kind-tagged
    "confirmed_relationship_edges" metadata key (core.sqlservice.
    retrieval_documents, Step 7). A document from an older build that
    predates this key (or a genuinely absent match — should not happen for
    a neighbor the graph itself already confirmed adjacency for) degrades
    to "OTHER_CONFIRMED", the lowest, never-a-crash-inducing default.
    """
    best_rank = 99
    best_kind = "OTHER_CONFIRMED"
    for edge in anchor_doc.metadata.get("confirmed_relationship_edges") or ():
        other = str(edge.get("other") or "")
        other_fqn, sep, _column = other.rpartition(".")
        if sep and other_fqn.lower() == neighbor_fqn_lower:
            rank = _RELATIONSHIP_KIND_PRIORITY.get(edge.get("kind"), 99)
            if rank < best_rank:
                best_rank = rank
                best_kind = edge.get("kind") or "OTHER_CONFIRMED"
    return best_kind


def _select_pool_exempt_neighbors(
    *,
    anchor_keys: Sequence[str],
    graph,
    already_claimed: set[str],
    documents_by_fqn_lower: Mapping[str, IndexedSchemaDocument],
    lexical_score_by_key: Mapping[str, float],
    semantic_rank_by_key: Mapping[str, int],
    lexical_rank_by_key: Mapping[str, int],
    max_per_anchor: int,
    max_total: int,
) -> list[dict]:
    """
    One hop only, from anchors only (never recursing into a pool-exempt
    candidate's own neighbors — "must not trigger unbounded recursive graph
    expansion"). Returns an ordered list of dicts: {"key", "source_anchor_
    fqn", "kind", "touched_anchor_count", "reason"} — the ORDER already
    reflects overall admission order (anchor order, then per-anchor rank).

    Ranking within one anchor's eligible neighbor set (requirement 3):
    question-token/field relevance (lexical BM25F score — 0.0 when the
    neighbor scored no lexical match) first, then relationship provenance
    (physical FK > catalog view dependency > other confirmed), then
    neighbor fused rank where available (reciprocal-rank fusion of semantic
    + lexical rank — 0.0 when the neighbor appeared in neither), then
    bridge/path usefulness (how many DISTINCT anchors this neighbor is
    directly confirmed-adjacent to — never itself a recursive expansion,
    just a count over the already-fixed anchor set), then deterministic FQN
    tie-breaking.
    """
    if max_total <= 0 or max_per_anchor <= 0:
        return []

    claimed = set(already_claimed)
    admitted: list[dict] = []

    def sort_key(neighbor_key: str, anchor_key: str) -> tuple:
        kind = _confirmed_edge_kind(documents_by_fqn_lower[anchor_key], neighbor_key)
        provenance_rank = _RELATIONSHIP_KIND_PRIORITY.get(kind, 99)
        relevance = lexical_score_by_key.get(neighbor_key, 0.0)
        fused = _fuse_ranks(semantic_rank_by_key.get(neighbor_key), lexical_rank_by_key.get(neighbor_key))
        touched = sum(1 for a in anchor_keys if neighbor_key in graph.adjacency.get(a, ()))
        neighbor_fqn = documents_by_fqn_lower[neighbor_key].object_fqn
        return (-relevance, provenance_rank, -fused, -touched, neighbor_fqn)

    for anchor_key in anchor_keys:
        if len(admitted) >= max_total:
            break
        anchor_fqn = documents_by_fqn_lower[anchor_key].object_fqn
        eligible = sorted(
            (n for n in graph.adjacency.get(anchor_key, ()) if n not in claimed),
            key=lambda n: sort_key(n, anchor_key),
        )
        admitted_for_anchor = 0
        for neighbor_key in eligible:
            if admitted_for_anchor >= max_per_anchor or len(admitted) >= max_total:
                break
            kind = _confirmed_edge_kind(documents_by_fqn_lower[anchor_key], neighbor_key)
            touched_anchors = [
                documents_by_fqn_lower[a].object_fqn
                for a in anchor_keys
                if neighbor_key in graph.adjacency.get(a, ())
            ]
            if len(touched_anchors) > 1:
                reason = (
                    f"confirmed direct neighbor of {len(touched_anchors)} anchors "
                    f"(incl. {anchor_fqn!r}) via {kind}, outside candidate pool — "
                    f"may bridge {touched_anchors!r}"
                )
            else:
                reason = (
                    f"confirmed direct neighbor of anchor {anchor_fqn!r} via {kind}, "
                    "outside candidate pool"
                )
            admitted.append({
                "key": neighbor_key,
                "source_anchor_fqn": anchor_fqn,
                "kind": kind,
                "touched_anchor_count": len(touched_anchors),
                "reason": reason,
            })
            claimed.add(neighbor_key)
            admitted_for_anchor += 1

    return admitted


# ── Public entry point ────────────────────────────────────────────────────


def _fuse_ranks(
    semantic_rank: int | None,
    lexical_rank: int | None,
    canonical_rank: int | None = None,
    *,
    canonical_weight: float = 0.0,
    business_concept_rank: int | None = None,
    business_concept_weight: float = 0.0,
) -> float:
    """
    Reciprocal Rank Fusion (see RRF_K's own docstring above). A rank of
    None (the document never appeared in that ranking) simply contributes
    no term — never a fabricated worst-case rank — so a document present
    in only one ranking is fused purely from that one term, and a document
    present in neither (impossible for a semantic-tier-filtered candidate,
    since every one of those always has a semantic_rank) never occurs.

    Phase 5, Step 8: `canonical_rank`/`canonical_weight` are optional and
    default to a no-op (None / 0.0) — every existing caller that never
    passes them keeps computing exactly the pre-Step-8 two-term score. When
    supplied, the canonical term uses the IDENTICAL bounded shape as the
    semantic/lexical terms (weight / (RRF_K + rank)), just scaled by
    `canonical_weight` — see DEFAULT_CANONICAL_PRIOR_WEIGHT's own docstring
    for why that weight is deliberately < 1.0.

    Phase 7, Step 3B: `business_concept_rank`/`business_concept_weight`
    follow the identical shape and the identical no-op default (None / 0.0)
    — every caller that never passes them keeps computing exactly the
    pre-Step-3B score. See DEFAULT_BUSINESS_CONCEPT_WEIGHT's own docstring
    for why, unlike canonical_weight, this term defaults to FULL (1.0)
    parity with semantic/lexical when a caller does enable it.
    """
    score = 0.0
    if semantic_rank is not None:
        score += 1.0 / (RRF_K + semantic_rank)
    if lexical_rank is not None:
        score += 1.0 / (RRF_K + lexical_rank)
    if canonical_rank is not None and canonical_weight:
        score += canonical_weight / (RRF_K + canonical_rank)
    if business_concept_rank is not None and business_concept_weight:
        score += business_concept_weight / (RRF_K + business_concept_rank)
    return score


_EMPTY_RANK_COMPONENTS: Mapping[str, object] = {
    "score": None, "semantic_rank": None, "lexical_rank": None,
    "lexical_score": None, "fused_score": None, "matched_lexical_fields": (),
    "business_concept_rank": None, "business_concept_score": None, "matched_business_concepts": (),
}


def _retrieve_schema_context_with_vector(
    question: str,
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    top_k: int = DEFAULT_TOP_K,
    relationship_limit: int = DEFAULT_RELATIONSHIP_LIMIT,
    max_context_characters: int = DEFAULT_MAX_CONTEXT_CHARACTERS,
    allowed_tiers: frozenset[int] | None = None,
    ranking_mode: str = DEFAULT_RANKING_MODE,
    candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    max_relationship_path_length: int = DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH,
    max_projected_columns_per_object: int = DEFAULT_MAX_PROJECTED_COLUMNS,
    max_pool_exempt_neighbors_per_anchor: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR,
    max_pool_exempt_neighbors_total: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL,
    enable_canonical_prior: bool = DEFAULT_ENABLE_CANONICAL_PRIOR,
    canonical_prior_weight: float = DEFAULT_CANONICAL_PRIOR_WEIGHT,
    enable_business_concept_retrieval: bool = DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL,
    business_concept_weight: float = DEFAULT_BUSINESS_CONCEPT_WEIGHT,
    example_proposed_objects: Sequence[tuple[str, int, str]] = (),
) -> tuple[SchemaContextResult, tuple[float, ...] | None]:
    """
    Private. The real implementation behind both public
    retrieve_schema_context() (which returns only the first tuple element)
    and retrieve_schema_context_and_examples() (which reuses the second —
    the query vector — for example retrieval, internally, so the whole
    package still makes exactly one embedding call per question). Never
    imported outside core.sqlservice — the query vector this function's
    second return value carries must never reach a route/adapter; see
    SchemaContextResult's own docstring for why.

    Convert one question into one embedding, search the build's vector
    index, fuse it with a deterministic lexical (BM25F) ranking over the
    same build, then (RANKING_MODE_HYBRID only) run Phase 5 Step 5's
    candidate-pool / relationship-graph / size-aware packing pipeline;
    RANKING_MODE_VECTOR keeps the exact pre-Step-5 simple behavior
    unchanged (top_k selection, one-hop relationship expansion, first-fit
    packing) as a preserved comparison mode.

    Order of operations (cheapest/safest checks first — a provider call is
    never made unless everything before it already succeeded):
      1. Validate the question and every numeric/tier/mode parameter —
         raises ValueError, no I/O yet.
      2. load_vector_index() — the ownership/READY/completeness gate. A
         non-OK status short-circuits here (zero provider calls) and is
         propagated unchanged as this result's `status`/`error`.
      3. provider.embed_batch([question]) — exactly one call, one input.
      4. search_vector_index() over the WHOLE index, then keep only
         `allowed_tiers` documents — tier filtering always happens before
         any ranking cutoff.
      5. RANKING_MODE_HYBRID only: BM25F-rank the same tier-filtered
         documents (no SQLite access — purely in-memory over already-
         loaded documents) and fuse with the semantic ranking via
         reciprocal rank fusion (see _fuse_ranks). RANKING_MODE_VECTOR
         skips this — fusion then reduces to the semantic term alone, a
         strictly rank-order-preserving transform of the original
         ordering, so RANKING_MODE_VECTOR's behavior is unchanged.

    RANKING_MODE_VECTOR (steps 6V-8V): take the fused top `top_k` ->
    one-hop confirmed-relationship expansion from those, capped at
    `relationship_limit` -> pack fused-score-descending then relationship
    documents into the character budget, first-fit, never compacted.

    RANKING_MODE_HYBRID (steps 6H-9H) — Phase 5 Step 5:
      6H. candidate pool = fused top `candidate_pool_size` (>= top_k);
          anchors = the pool's own top `top_k`; spillover = the rest.
      7H. build an in-memory confirmed-relationship graph (never inferred
          edges) over every tier-allowed document — see
          core.sqlservice.relationship_graph — then: (a) bridges — for
          each consecutive pair of anchors in fused-rank order, the
          shortest confirmed path up to `max_relationship_path_length`
          edges (hard-capped at 3); every intermediate node is a
          relationship-bridge candidate; (b) directs — one confirmed hop
          from each anchor, for whatever nodes aren't already anchors/
          bridges. Both combined are capped at `relationship_limit`,
          bridges prioritized first.
      8H. pack anchors, then bridges, then directs, then spillover — in
          that strict priority order — via core.sqlservice.context_packing.
          pack_candidates(): a candidate always gets first claim on the
          budget in full-document form; if it doesn't fit, an
          anchor/bridge/direct (never spillover) falls back to a compact,
          purely structured-metadata projection instead of being dropped.
          This is what guarantees an essential anchor or a required join
          bridge is never displaced merely because an earlier-priority
          candidate happened to be large.
      9H. context_text is the packed items' text (full or compact) joined
          in packing order; ContextDocument.document_text/document_hash
          always carry the ORIGINAL full document regardless of which
          text was actually packed (see used_compact_text).

      Phase 5, Step 7, Part 2 (runs between 7H and 8H): for every anchor,
      inspect confirmed direct neighbors from the FULL tier-allowed graph —
      not only ones already inside the candidate pool — and admit up to
      `max_pool_exempt_neighbors_per_anchor` per anchor /
      `max_pool_exempt_neighbors_total` overall (both 0 disables this
      entirely), ranked by question-token/field relevance, relationship
      provenance (physical FK > catalog view dependency > other confirmed),
      neighbor fused rank where available, and bridge/path usefulness
      (deterministic FQN tie-break) — see
      core.sqlservice.schema_context_retrieval._select_pool_exempt_neighbors.
      Packed with SELECTION_REASON_POOL_EXEMPT_NEIGHBOR, lowest priority
      (after anchors/bridges/pool-scoped directs, before nothing else
      currently exists). Never recurses past one hop from an anchor.

      Phase 5, Step 8 (runs immediately after 6H, before bridge/direct/
      pool-exempt discovery): when `enable_canonical_prior` (default True)
      and `canonical_prior_weight` (default DEFAULT_CANONICAL_PRIOR_WEIGHT,
      0.0 disables), core.sqlservice.canonical_scoring's per-build,
      question-independent canonical-authority score is folded into a
      SECOND fused ranking — via the same bounded RRF term shape, just a
      third term — used ONLY to pick which top_k of the ALREADY-fixed
      `pool_keys` become `anchor_keys`. Pool membership itself is untouched
      (still pure semantic+lexical relevance), so an object can never
      become an anchor "solely because of" a high canonical score — it
      must already be relevant enough to be in the pool. Bridge/direct/
      pool-exempt discovery below is completely unaffected — it operates
      on `anchor_keys` exactly as before, whatever those turned out to be.

    Raises:
        ValueError: invalid question, top_k, relationship_limit,
            max_context_characters, allowed_tiers, ranking_mode,
            candidate_pool_size (including < top_k),
            max_relationship_path_length (including > the hard cap),
            max_projected_columns_per_object,
            max_pool_exempt_neighbors_per_anchor,
            max_pool_exempt_neighbors_total, enable_canonical_prior,
            canonical_prior_weight (outside [0.0, 1.0]), or a budget too
            small to fit any candidate document (full or compact).
        SchemaRetrievalValidationError: the provider's response was
            malformed.
    """
    trimmed_question = _validate_question(question)

    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
    if (
        not isinstance(relationship_limit, int)
        or isinstance(relationship_limit, bool)
        or relationship_limit < 0
    ):
        raise ValueError(f"relationship_limit must be a non-negative integer, got {relationship_limit!r}")
    if (
        not isinstance(max_context_characters, int)
        or isinstance(max_context_characters, bool)
        or max_context_characters <= 0
    ):
        raise ValueError(f"max_context_characters must be a positive integer, got {max_context_characters!r}")
    if ranking_mode not in _ALLOWED_RANKING_MODES:
        raise ValueError(f"ranking_mode must be one of {sorted(_ALLOWED_RANKING_MODES)}, got {ranking_mode!r}")
    if (
        not isinstance(candidate_pool_size, int)
        or isinstance(candidate_pool_size, bool)
        or candidate_pool_size <= 0
    ):
        raise ValueError(f"candidate_pool_size must be a positive integer, got {candidate_pool_size!r}")
    if candidate_pool_size < top_k:
        raise ValueError(f"candidate_pool_size ({candidate_pool_size}) must be >= top_k ({top_k})")
    if (
        not isinstance(max_relationship_path_length, int)
        or isinstance(max_relationship_path_length, bool)
        or max_relationship_path_length <= 0
    ):
        raise ValueError(
            f"max_relationship_path_length must be a positive integer, got {max_relationship_path_length!r}"
        )
    if max_relationship_path_length > MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH:
        raise ValueError(
            f"max_relationship_path_length={max_relationship_path_length} exceeds the hard cap of "
            f"{MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH} edges"
        )
    if (
        not isinstance(max_projected_columns_per_object, int)
        or isinstance(max_projected_columns_per_object, bool)
        or max_projected_columns_per_object <= 0
    ):
        raise ValueError(
            f"max_projected_columns_per_object must be a positive integer, got {max_projected_columns_per_object!r}"
        )
    if (
        not isinstance(max_pool_exempt_neighbors_per_anchor, int)
        or isinstance(max_pool_exempt_neighbors_per_anchor, bool)
        or max_pool_exempt_neighbors_per_anchor < 0
    ):
        raise ValueError(
            "max_pool_exempt_neighbors_per_anchor must be a non-negative integer, got "
            f"{max_pool_exempt_neighbors_per_anchor!r}"
        )
    if (
        not isinstance(max_pool_exempt_neighbors_total, int)
        or isinstance(max_pool_exempt_neighbors_total, bool)
        or max_pool_exempt_neighbors_total < 0
    ):
        raise ValueError(
            f"max_pool_exempt_neighbors_total must be a non-negative integer, got {max_pool_exempt_neighbors_total!r}"
        )
    if not isinstance(enable_canonical_prior, bool):
        raise ValueError(f"enable_canonical_prior must be a bool, got {enable_canonical_prior!r}")
    if (
        isinstance(canonical_prior_weight, bool)
        or not isinstance(canonical_prior_weight, (int, float))
        or not (0.0 <= canonical_prior_weight <= 1.0)
    ):
        raise ValueError(
            f"canonical_prior_weight must be a number in [0.0, 1.0], got {canonical_prior_weight!r}"
        )
    if not isinstance(enable_business_concept_retrieval, bool):
        raise ValueError(
            f"enable_business_concept_retrieval must be a bool, got {enable_business_concept_retrieval!r}"
        )
    if (
        isinstance(business_concept_weight, bool)
        or not isinstance(business_concept_weight, (int, float))
        or not (0.0 <= business_concept_weight <= 1.0)
    ):
        raise ValueError(
            f"business_concept_weight must be a number in [0.0, 1.0], got {business_concept_weight!r}"
        )

    if allowed_tiers is None:
        effective_tiers = DEFAULT_ALLOWED_TIERS
    else:
        effective_tiers = frozenset(allowed_tiers)
        if not effective_tiers or not effective_tiers.issubset(_ALL_TIERS):
            raise ValueError(
                f"allowed_tiers must be a non-empty subset of {sorted(_ALL_TIERS)}, got {allowed_tiers!r}"
            )

    load_result = load_vector_index(
        build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    if load_result.status != STATUS_OK:
        return _empty_result(load_result.status, build_id, provider, load_result.error), None
    index = load_result.index

    response = provider.embed_batch([trimmed_question])
    query_vector = _validate_single_embedding_response(response, provider.model, provider.dimensions)

    ranked = search_vector_index(index, query_vector, top_k=len(index.documents) or 1)
    tier_filtered = [r for r in ranked if r.retrieval_tier in effective_tiers]

    documents_by_fqn_lower = {d.object_fqn.lower(): d for d in index.documents}
    semantic_rank_by_key = {r.object_fqn.lower(): i + 1 for i, r in enumerate(tier_filtered)}
    semantic_score_by_key = {r.object_fqn.lower(): r.score for r in tier_filtered}
    fqn_by_key = {r.object_fqn.lower(): r.object_fqn for r in tier_filtered}

    lexical_rank_by_key: dict[str, int] = {}
    lexical_score_by_key: dict[str, float] = {}
    matched_fields_by_key: dict[str, tuple[str, ...]] = {}
    business_concept_rank_by_key: dict[str, int] = {}
    business_concept_score_by_key: dict[str, float] = {}
    matched_business_concepts_by_key: dict[str, tuple] = {}
    query_tokens: tuple[str, ...] = ()

    if ranking_mode == RANKING_MODE_HYBRID:
        lexical_identity = LexicalIndexIdentity(
            build_id=build_id, embedding_provider=provider.provider_id,
            embedding_model=provider.model, embedding_dimensions=provider.dimensions,
        )
        lexical_index = load_lexical_index(index.documents, lexical_identity)
        query_tokens = tokenize(trimmed_question)
        if query_tokens:
            lexical_ranked = search_lexical_index(
                lexical_index, query_tokens, top_k=lexical_index.document_count or 1,
            )
            lexical_tier_filtered = [r for r in lexical_ranked if r.retrieval_tier in effective_tiers]
            for i, r in enumerate(lexical_tier_filtered):
                key = r.object_fqn.lower()
                lexical_rank_by_key[key] = i + 1
                lexical_score_by_key[key] = r.score
                matched_fields_by_key[key] = r.matched_fields

        # Phase 7, Step 3B, Part 2 — automatic business-concept retrieval.
        # Never a second embedding call (query_tokens are the SAME
        # deterministic tokens already computed for lexical search above);
        # never calls an AI provider; fails open to "no matches" for this
        # question on ANY internal error (see load_business_concept_index's
        # own docstring) so this optional lane can never break retrieval.
        if enable_business_concept_retrieval and business_concept_weight and query_tokens:
            business_concept_index = load_business_concept_index(
                build_id, source_id, user_id, index.documents, allowed_tiers=effective_tiers,
            )
            business_concept_ranked = search_business_concept_index(
                business_concept_index, query_tokens,
                top_k=business_concept_index.lexical_index.document_count or 1,
            )
            business_concept_tier_filtered = [
                r for r in business_concept_ranked
                if (documents_by_fqn_lower.get(r.object_fqn.lower()) is not None)
            ]
            for i, r in enumerate(business_concept_tier_filtered):
                key = r.object_fqn.lower()
                business_concept_rank_by_key[key] = i + 1
                business_concept_score_by_key[key] = r.score
                matched_business_concepts_by_key[key] = r.matched_evidence

    fused_keys_sorted = sorted(
        semantic_rank_by_key.keys(),
        key=lambda key: (
            -_fuse_ranks(
                semantic_rank_by_key[key], lexical_rank_by_key.get(key),
                business_concept_rank=business_concept_rank_by_key.get(key),
                business_concept_weight=business_concept_weight,
            ),
            fqn_by_key[key],
        ),
    )

    def _rank_components(key: str) -> dict:
        return {
            "score": semantic_score_by_key[key],
            "semantic_rank": semantic_rank_by_key[key],
            "lexical_rank": lexical_rank_by_key.get(key),
            "lexical_score": lexical_score_by_key.get(key),
            "fused_score": _fuse_ranks(
                semantic_rank_by_key[key], lexical_rank_by_key.get(key),
                business_concept_rank=business_concept_rank_by_key.get(key),
                business_concept_weight=business_concept_weight,
            ),
            "matched_lexical_fields": matched_fields_by_key.get(key, ()),
            "business_concept_rank": business_concept_rank_by_key.get(key),
            "business_concept_score": business_concept_score_by_key.get(key),
            "matched_business_concepts": matched_business_concepts_by_key.get(key, ()),
        }

    if ranking_mode == RANKING_MODE_VECTOR:
        # ── Preserved pre-Step-5 pure-vector behavior, unchanged ────────
        # Phase 7, Step 3A, Part 2's connectivity-aware anchor packing and
        # essential_context_incomplete signal are RANKING_MODE_HYBRID-only
        # additions (they depend on that mode's own candidate-pool/graph
        # pipeline) — RANKING_MODE_VECTOR never computes either, so this
        # always reports False, exactly like every prior release.
        essential_context_incomplete = False
        selected_keys = fused_keys_sorted[:top_k]
        selected_fqns_lower = set(selected_keys)

        relationship_docs: list[IndexedSchemaDocument] = []
        seen_relationship_fqns_lower: set[str] = set()
        for key in selected_keys:
            if len(relationship_docs) >= relationship_limit:
                break
            source_doc = documents_by_fqn_lower[key]
            for candidate_fqn in _relationship_candidate_fqns(source_doc):
                if len(relationship_docs) >= relationship_limit:
                    break
                rel_key = candidate_fqn.lower()
                if rel_key == key:
                    continue
                if rel_key in selected_fqns_lower or rel_key in seen_relationship_fqns_lower:
                    continue
                candidate_doc = documents_by_fqn_lower.get(rel_key)
                if candidate_doc is None:
                    continue  # dangling reference — not part of this index
                if candidate_doc.retrieval_tier not in effective_tiers:
                    continue
                relationship_docs.append(candidate_doc)
                seen_relationship_fqns_lower.add(rel_key)

        candidates: list[tuple[IndexedSchemaDocument, str, dict]] = [
            (documents_by_fqn_lower[key], SELECTION_REASON_SEMANTIC, _rank_components(key))
            for key in selected_keys
        ] + [
            (doc, SELECTION_REASON_RELATIONSHIP, dict(_EMPTY_RANK_COMPONENTS))
            for doc in relationship_docs
        ]

        selected: list[ContextDocument] = []
        text_parts: list[str] = []
        used_characters = 0
        warnings: list[str] = []
        truncated = False

        for doc, reason, components in candidates:
            addition = len(doc.document_text) + (len(_CONTEXT_SEPARATOR) if text_parts else 0)
            if used_characters + addition > max_context_characters:
                truncated = True
                warnings.append(
                    f"skipped {doc.object_fqn!r} ({reason}): would exceed the {max_context_characters}-character budget"
                )
                continue
            selected.append(
                ContextDocument(
                    object_fqn=doc.object_fqn, object_type=doc.object_type, retrieval_tier=doc.retrieval_tier,
                    document_hash=doc.document_hash, document_text=doc.document_text, metadata=doc.metadata,
                    score=components["score"], selection_reason=reason,
                    semantic_rank=components["semantic_rank"], lexical_rank=components["lexical_rank"],
                    lexical_score=components["lexical_score"], fused_score=components["fused_score"],
                    matched_lexical_fields=components["matched_lexical_fields"],
                )
            )
            text_parts.append(doc.document_text)
            used_characters += addition

        if not selected and candidates:
            smallest = min(len(doc.document_text) for doc, _reason, _components in candidates)
            raise ValueError(
                f"max_context_characters={max_context_characters} is too small to fit any selected "
                f"document (smallest candidate is {smallest} characters)"
            )
        if not candidates:
            warnings.append("no documents matched the allowed retrieval tiers for this question")

        context_text = _CONTEXT_SEPARATOR.join(text_parts)

    else:
        # ── Phase 5, Step 5: candidate pool -> anchors -> relationship
        #    graph -> bounded bridge/direct coverage -> size-aware packing.
        # The pool is the bounded, adaptive working set anchors AND direct-
        # relationship candidates are drawn from (requirement: "larger than
        # the final requested object count" — candidate_pool_size >= top_k
        # is enforced above). Bridges are deliberately NOT pool-restricted:
        # a bridge table required to connect two anchors is structurally
        # necessary regardless of its own fused rank (that is exactly the
        # Step 4/4B-identified gap this step closes), so bridge discovery
        # searches the full tier-allowed relationship graph, never just the
        # pool — "never expand the entire graph" is instead enforced by
        # max_relationship_path_length and relationship_limit, not by
        # restricting which nodes the graph itself contains.
        pool_keys = fused_keys_sorted[:candidate_pool_size]
        pool_set = set(pool_keys)

        # Phase 5, Step 8 — canonical-object scoring as a bounded relevance
        # prior for ANCHOR SELECTION only (requirements 2/3/6). Pool
        # MEMBERSHIP above is already fixed by relevance alone; canonical
        # scoring only ever re-ranks WITHIN that already-relevant set to
        # decide which top_k become anchors — an object with no query
        # relevance can never reach `pool_keys` in the first place, so it
        # can never become an anchor "solely because of its canonical
        # score" regardless of how authoritative it is build-wide.
        canonical_result: CanonicalScoringResult | None = None
        pre_canonical_pool_rank: dict[str, int] = {}
        post_canonical_pool_rank: dict[str, int] = {}
        if enable_canonical_prior and canonical_prior_weight > 0.0:
            canonical_identity = CanonicalScoringIdentity(
                build_id=build_id, embedding_provider=provider.provider_id,
                embedding_model=provider.model, embedding_dimensions=provider.dimensions,
            )
            canonical_result = load_canonical_scores(index.documents, canonical_identity)
            canonical_rank_by_key = canonical_result.rank_by_fqn_lower

            pre_canonical_pool_rank = {key: i + 1 for i, key in enumerate(pool_keys)}
            canonical_sorted_pool = sorted(
                pool_keys,
                key=lambda k: (
                    -_fuse_ranks(
                        semantic_rank_by_key[k], lexical_rank_by_key.get(k),
                        canonical_rank_by_key.get(k), canonical_weight=canonical_prior_weight,
                        business_concept_rank=business_concept_rank_by_key.get(k),
                        business_concept_weight=business_concept_weight,
                    ),
                    fqn_by_key[k],
                ),
            )
            post_canonical_pool_rank = {key: i + 1 for i, key in enumerate(canonical_sorted_pool)}
            anchor_keys = canonical_sorted_pool[:top_k]
        else:
            anchor_keys = pool_keys[:top_k]

        anchor_and_bridge_keys = set(anchor_keys)

        graph_documents = [d for d in index.documents if d.retrieval_tier in effective_tiers]
        graph = build_relationship_graph(graph_documents)

        # Phase 7, Step 3A, Part 2 — connectivity-aware anchor packing
        # priority. An anchor that is itself a CONFIRMED direct neighbor of
        # another anchor (i.e. the two are already joinable via a real
        # relationship edge) is a REQUIRED CONNECTOR for this question's own
        # selected object set, not merely "one more relevant table" — it
        # must not be silently squeezed out of the character budget by
        # other, lower-priority anchors that happen to rank higher on raw
        # fused relevance alone but connect to nothing else selected (the
        # verified Step 3 failure: a low-ranked-but-structurally-essential
        # anchor was dropped while several disconnected, merely-topical
        # anchors consumed the whole budget ahead of it).
        #
        # `connected_anchor_keys` (packed FIRST, same SELECTION_REASON_
        # HYBRID_ANCHOR as before — this never changes WHAT counts as an
        # anchor, only in what order they compete for budget) are anchors
        # with at least one confirmed-relationship edge to another anchor in
        # THIS SAME selected set. `isolated_anchor_keys` (packed LAST, right
        # before example-derived objects) are anchors with no such edge —
        # still fully eligible, just no longer able to starve out a
        # structurally connected anchor purely by relevance-rank position.
        # Reuses ONLY the existing confirmed-relationship graph (`graph`,
        # built above from confirmed edges alone, tier-filtered exactly as
        # before) — no schema_linker/join_planner/schema_plan_options/
        # view_authority call, and no notion of "business intent" beyond
        # "is this anchor factually, confirmed-relationship-adjacent to
        # another anchor already selected" — a structural fact, not a
        # semantic judgment.
        connected_anchor_keys: list[str] = []
        isolated_anchor_keys: list[str] = []
        for key in anchor_keys:
            if anchor_and_bridge_keys & graph.adjacency.get(key, frozenset()):
                connected_anchor_keys.append(key)
            else:
                isolated_anchor_keys.append(key)

        bridge_keys: list[str] = []
        for i in range(len(anchor_keys) - 1):
            if len(bridge_keys) >= relationship_limit:
                break
            path = find_shortest_relationship_path(
                graph, anchor_keys[i], anchor_keys[i + 1], max_path_length=max_relationship_path_length,
            )
            if path is None:
                continue
            for node in path[1:-1]:
                if len(bridge_keys) >= relationship_limit:
                    break
                if node in anchor_and_bridge_keys:
                    continue
                bridge_keys.append(node)
                anchor_and_bridge_keys.add(node)

        direct_keys: list[str] = []
        seen_direct = set(anchor_and_bridge_keys)
        for anchor_key in anchor_keys:
            if len(bridge_keys) + len(direct_keys) >= relationship_limit:
                break
            for neighbor_key in sorted(graph.adjacency.get(anchor_key, ())):
                if len(bridge_keys) + len(direct_keys) >= relationship_limit:
                    break
                if neighbor_key in seen_direct:
                    continue
                if neighbor_key not in pool_set:
                    continue  # direct-relationship candidates are pool-scoped; bridges are not
                direct_keys.append(neighbor_key)
                seen_direct.add(neighbor_key)

        # Phase 5, Step 7, Part 2 — pool-exempt confirmed neighbors. Unlike
        # direct_keys above (pool-scoped), this pass inspects every anchor's
        # confirmed direct neighbors in the FULL tier-allowed graph, so a
        # structurally essential neighbor the relevance-only pool cutoff
        # excluded can still be admitted — bounded per-anchor and in total,
        # never recursing past one hop from an anchor (see
        # _select_pool_exempt_neighbors's own docstring).
        pool_exempt_admissions = _select_pool_exempt_neighbors(
            anchor_keys=anchor_keys,
            graph=graph,
            already_claimed=seen_direct | set(direct_keys),
            documents_by_fqn_lower=documents_by_fqn_lower,
            lexical_score_by_key=lexical_score_by_key,
            semantic_rank_by_key=semantic_rank_by_key,
            lexical_rank_by_key=lexical_rank_by_key,
            max_per_anchor=max_pool_exempt_neighbors_per_anchor,
            max_total=max_pool_exempt_neighbors_total,
        )

        # Phase 5, Step 8, requirement 7 — full canonical observability on
        # every packed document that has a canonical score, regardless of
        # WHY it was selected; `is_anchor` gates ONLY the
        # canonical_changed_anchor_rank field, since canonical scoring
        # never influences bridge/direct/pool-exempt selection itself
        # (requirement 6).
        def _canonical_observability(key: str, *, is_anchor: bool) -> dict:
            if canonical_result is None:
                return {}
            score_obj = canonical_result.scores_by_fqn_lower.get(key)
            if score_obj is None:
                return {}
            competing = tuple(sorted(
                documents_by_fqn_lower[other].object_fqn
                for other in pool_keys
                if other != key
                and canonical_result.scores_by_fqn_lower.get(other) is not None
                and canonical_result.scores_by_fqn_lower[other].family_key == score_obj.family_key
            ))
            return {
                "canonical_total_score": score_obj.total_score,
                "canonical_components": score_obj.components,
                "canonical_rank": canonical_result.rank_by_fqn_lower.get(key),
                "canonical_family_key": score_obj.family_key,
                "canonical_family_size": score_obj.family_size,
                "canonical_family_rank": score_obj.family_rank,
                "canonical_competing_fqns": competing,
                "canonical_changed_anchor_rank": (
                    (pre_canonical_pool_rank.get(key) != post_canonical_pool_rank.get(key))
                    if is_anchor else None
                ),
            }

        pack_input: list[PackCandidate] = []
        for key in connected_anchor_keys:
            components = _rank_components(key)
            components.update(_canonical_observability(key, is_anchor=True))
            pack_input.append(PackCandidate(
                doc=documents_by_fqn_lower[key], reason=SELECTION_REASON_HYBRID_ANCHOR,
                components=components, allow_compact=True,
            ))
        for key in bridge_keys:
            components = dict(_EMPTY_RANK_COMPONENTS)
            components.update(_canonical_observability(key, is_anchor=False))
            pack_input.append(PackCandidate(
                doc=documents_by_fqn_lower[key], reason=SELECTION_REASON_RELATIONSHIP_BRIDGE,
                components=components, allow_compact=True,
            ))
        for key in direct_keys:
            components = dict(_EMPTY_RANK_COMPONENTS)
            components.update(_canonical_observability(key, is_anchor=False))
            pack_input.append(PackCandidate(
                doc=documents_by_fqn_lower[key], reason=SELECTION_REASON_DIRECT_RELATIONSHIP,
                components=components, allow_compact=True,
            ))
        for admission in pool_exempt_admissions:
            components = _rank_components(admission["key"])
            components["source_anchor_fqn"] = admission["source_anchor_fqn"]
            components["pool_exempt_reason"] = admission["reason"]
            components.update(_canonical_observability(admission["key"], is_anchor=False))
            pack_input.append(PackCandidate(
                doc=documents_by_fqn_lower[admission["key"]], reason=SELECTION_REASON_POOL_EXEMPT_NEIGHBOR,
                components=components, allow_compact=True,
            ))

        # Phase 7, Step 3A, Part 2 — isolated anchors (relevant on their own
        # terms, but confirmed-adjacent to nothing else in this selected
        # set) pack LAST among the anchor/bridge/direct/pool-exempt tiers —
        # "optional supporting objects" in the priority order this fix
        # implements, right alongside (and before) example-derived objects.
        # Still fully eligible, still SELECTION_REASON_HYBRID_ANCHOR, still
        # subject to the exact same compact-fallback/skip rules as every
        # other candidate — this only ever changes ORDER, never eligibility.
        for key in isolated_anchor_keys:
            components = _rank_components(key)
            components.update(_canonical_observability(key, is_anchor=True))
            pack_input.append(PackCandidate(
                doc=documents_by_fqn_lower[key], reason=SELECTION_REASON_HYBRID_ANCHOR,
                components=components, allow_compact=True,
            ))

        # Phase 5, Step 9 — example-derived objects. `example_proposed_objects`
        # is (object_fqn, example_id, reason) triples the CALLER already
        # computed via core.sqlservice.sql_example_retrieval.retrieve_sql_examples
        # (trust/similarity-threshold/compatibility already verified there —
        # this function never re-derives trust, but DOES defensively
        # re-verify tier and existence/dedup here, since schema state is
        # exactly what this function already has loaded). Lowest packing
        # priority of all five reasons; never recurses; still fully subject
        # to tier filtering and the normal context-budget/packing rules
        # below (requirement: "normal tier and context-budget rules still
        # pass").
        claimed_all = anchor_and_bridge_keys | set(direct_keys) | {a["key"] for a in pool_exempt_admissions}
        seen_example_keys: set[str] = set()
        for object_fqn, example_id, reason in example_proposed_objects:
            key = object_fqn.lower()
            if key in claimed_all or key in seen_example_keys:
                continue
            doc = documents_by_fqn_lower.get(key)
            if doc is None or doc.retrieval_tier not in effective_tiers:
                continue
            seen_example_keys.add(key)
            components = dict(_EMPTY_RANK_COMPONENTS)
            components["example_id"] = example_id
            components["example_derived_reason"] = reason
            components.update(_canonical_observability(key, is_anchor=False))
            pack_input.append(PackCandidate(
                doc=doc, reason=SELECTION_REASON_EXAMPLE_DERIVED, components=components, allow_compact=True,
            ))

        pack_result = pack_candidates(
            pack_input, max_context_characters=max_context_characters,
            query_tokens=query_tokens, max_projected_columns=max_projected_columns_per_object,
        )

        if not pack_result.packed and pack_input:
            smallest = min(len(c.doc.document_text) for c in pack_input)
            raise ValueError(
                f"max_context_characters={max_context_characters} is too small to fit any selected "
                f"document, full or compact (smallest full candidate is {smallest} characters)"
            )

        # Phase 7, Step 3A, Part 2, requirement 8 — an explicit, distinct
        # signal when a REQUIRED object (a connected anchor, or a required
        # confirmed-relationship bridge — the two "essential" packing tiers
        # this fix protects) could not be packed at all, even compactly,
        # because the character budget was exhausted by earlier essential
        # candidates themselves. Never silently presented as a sufficient
        # context: this is reported separately from the generic per-
        # candidate skip warnings pack_candidates() already emits (those
        # fire for ANY skipped candidate, essential or not) and surfaces as
        # its own SchemaContextResult.essential_context_incomplete flag plus
        # a distinctly-prefixed warning naming exactly which essential
        # object(s) were lost.
        packed_keys_lower = {item.doc.object_fqn.lower() for item in pack_result.packed}
        essential_keys = set(connected_anchor_keys) | set(bridge_keys)
        missing_essential_keys = sorted(essential_keys - packed_keys_lower)
        essential_context_incomplete = bool(missing_essential_keys)

        selected = [
            ContextDocument(
                object_fqn=item.doc.object_fqn, object_type=item.doc.object_type,
                retrieval_tier=item.doc.retrieval_tier, document_hash=item.doc.document_hash,
                document_text=item.doc.document_text, metadata=item.doc.metadata,
                score=item.components["score"], selection_reason=item.reason,
                semantic_rank=item.components["semantic_rank"], lexical_rank=item.components["lexical_rank"],
                lexical_score=item.components["lexical_score"], fused_score=item.components["fused_score"],
                matched_lexical_fields=item.components["matched_lexical_fields"],
                used_compact_text=item.used_compact_text,
                source_anchor_fqn=item.components.get("source_anchor_fqn"),
                pool_exempt_reason=item.components.get("pool_exempt_reason"),
                canonical_total_score=item.components.get("canonical_total_score"),
                canonical_components=item.components.get("canonical_components"),
                canonical_rank=item.components.get("canonical_rank"),
                canonical_family_key=item.components.get("canonical_family_key"),
                canonical_family_size=item.components.get("canonical_family_size"),
                canonical_family_rank=item.components.get("canonical_family_rank"),
                canonical_competing_fqns=item.components.get("canonical_competing_fqns"),
                canonical_changed_anchor_rank=item.components.get("canonical_changed_anchor_rank"),
                example_id=item.components.get("example_id"),
                example_derived_reason=item.components.get("example_derived_reason"),
                business_concept_rank=item.components.get("business_concept_rank"),
                business_concept_score=item.components.get("business_concept_score"),
                matched_business_concepts=item.components.get("matched_business_concepts") or (),
            )
            for item in pack_result.packed
        ]
        context_text = _CONTEXT_SEPARATOR.join(item.context_text for item in pack_result.packed)
        truncated = pack_result.truncated
        warnings = list(pack_result.warnings)
        if not pack_input:
            warnings.append("no documents matched the allowed retrieval tiers for this question")
        if missing_essential_keys:
            missing_fqns = sorted(documents_by_fqn_lower[k].object_fqn for k in missing_essential_keys)
            warnings.append(
                "INCOMPLETE_CONTEXT: essential object(s) "
                f"{missing_fqns!r} (a connected anchor or a required confirmed-relationship connector) "
                "could not be packed into context, even compactly, within the character budget — the "
                "returned context may be insufficient to produce a fully confirmed-joinable query"
            )

    build_row = get_retrieval_build(build_id, source_id, user_id)
    schema_fingerprint = build_row.schema_fingerprint if build_row is not None else None

    return (
        SchemaContextResult(
            status=STATUS_OK,
            build_id=build_id,
            schema_fingerprint=schema_fingerprint,
            embedding_provider=provider.provider_id,
            embedding_model=provider.model,
            embedding_dimensions=provider.dimensions,
            documents=tuple(selected),
            context_text=context_text,
            total_characters=len(context_text),
            truncated=truncated,
            warnings=tuple(warnings),
            error=None,
            ranking_mode=ranking_mode,
            essential_context_incomplete=essential_context_incomplete,
        ),
        query_vector,
    )


# ── Public entry points (Step 9A: query-vector encapsulation) ─────────────


def retrieve_schema_context(
    question: str,
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    top_k: int = DEFAULT_TOP_K,
    relationship_limit: int = DEFAULT_RELATIONSHIP_LIMIT,
    max_context_characters: int = DEFAULT_MAX_CONTEXT_CHARACTERS,
    allowed_tiers: frozenset[int] | None = None,
    ranking_mode: str = DEFAULT_RANKING_MODE,
    candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    max_relationship_path_length: int = DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH,
    max_projected_columns_per_object: int = DEFAULT_MAX_PROJECTED_COLUMNS,
    max_pool_exempt_neighbors_per_anchor: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR,
    max_pool_exempt_neighbors_total: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL,
    enable_canonical_prior: bool = DEFAULT_ENABLE_CANONICAL_PRIOR,
    canonical_prior_weight: float = DEFAULT_CANONICAL_PRIOR_WEIGHT,
    enable_business_concept_retrieval: bool = DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL,
    business_concept_weight: float = DEFAULT_BUSINESS_CONCEPT_WEIGHT,
    example_proposed_objects: Sequence[tuple[str, int, str]] = (),
) -> SchemaContextResult:
    """
    Public entry point — identical behavior, arguments, and return shape to
    every prior release of this function (see
    _retrieve_schema_context_with_vector's own docstring, copied from this
    function's original one, for the full order-of-operations description
    of both ranking modes). The ONLY Step 9A change is that the query
    vector this call's one provider.embed_batch() call produces is never
    exposed on the returned SchemaContextResult — see that dataclass's own
    docstring, and retrieve_schema_context_and_examples() below for the
    internal-only flow that reuses it for example retrieval instead.
    """
    schema_context, _query_vector = _retrieve_schema_context_with_vector(
        question, build_id, source_id, user_id, provider,
        top_k=top_k, relationship_limit=relationship_limit, max_context_characters=max_context_characters,
        allowed_tiers=allowed_tiers, ranking_mode=ranking_mode, candidate_pool_size=candidate_pool_size,
        max_relationship_path_length=max_relationship_path_length,
        max_projected_columns_per_object=max_projected_columns_per_object,
        max_pool_exempt_neighbors_per_anchor=max_pool_exempt_neighbors_per_anchor,
        max_pool_exempt_neighbors_total=max_pool_exempt_neighbors_total,
        enable_canonical_prior=enable_canonical_prior, canonical_prior_weight=canonical_prior_weight,
        enable_business_concept_retrieval=enable_business_concept_retrieval,
        business_concept_weight=business_concept_weight,
        example_proposed_objects=example_proposed_objects,
    )
    return schema_context


EXAMPLE_RETRIEVAL_NOT_ATTEMPTED = "NOT_ATTEMPTED"  # schema retrieval itself wasn't STATUS_OK, or retrieve_examples=False -- examples were never even searched for


@dataclass(frozen=True)
class SchemaContextWithExamplesResult:
    """
    Result of one retrieve_schema_context_and_examples() call — the
    Step 9A internal-only combined retrieval flow. The question's
    embedding is created exactly ONCE (inside
    _retrieve_schema_context_with_vector) and reused for example retrieval
    — never a second provider.embed_batch() call — and the vector itself
    never appears anywhere on this result or on `schema_context` (see
    SchemaContextResult's own docstring).

    schema_context: byte-identical to what retrieve_schema_context() would
        have returned for the same arguments — this combined flow changes
        nothing about schema retrieval itself, only adds example retrieval
        alongside it using the same underlying computation. In particular,
        an empty (or absent) example index never changes this field at
        all — the Step 9A "byte-for-byte" requirement.
    examples: core.sqlservice.sql_example_retrieval.RetrievedSqlExample
        tuple — already trust-filtered (TRUSTED, ACTIVE only, enforced at
        example-index-build time), schema/tier-compatibility-checked, and
        similarity-ranked. Safe retrieved-example RECORDS and selection
        observability only (similarity score, proposed/unavailable
        objects) — never a raw vector anywhere in this shape. Empty
        whenever example_retrieval_status is not OK.
    example_retrieval_status: one of core.sqlservice.sql_example_retrieval's
        own STATUS_* constants (STATUS_OK / STATUS_UNOWNED /
        STATUS_NO_EXAMPLES), or this module's own
        EXAMPLE_RETRIEVAL_NOT_ATTEMPTED when schema retrieval itself did
        not reach STATUS_OK (no query vector was ever produced to reuse)
        or the caller passed retrieve_examples=False.
    """

    schema_context: SchemaContextResult
    examples: tuple
    example_retrieval_status: str


def retrieve_schema_context_and_examples(
    question: str,
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    top_k: int = DEFAULT_TOP_K,
    relationship_limit: int = DEFAULT_RELATIONSHIP_LIMIT,
    max_context_characters: int = DEFAULT_MAX_CONTEXT_CHARACTERS,
    allowed_tiers: frozenset[int] | None = None,
    ranking_mode: str = DEFAULT_RANKING_MODE,
    candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    max_relationship_path_length: int = DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH,
    max_projected_columns_per_object: int = DEFAULT_MAX_PROJECTED_COLUMNS,
    max_pool_exempt_neighbors_per_anchor: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_PER_ANCHOR,
    max_pool_exempt_neighbors_total: int = DEFAULT_MAX_POOL_EXEMPT_NEIGHBORS_TOTAL,
    enable_canonical_prior: bool = DEFAULT_ENABLE_CANONICAL_PRIOR,
    canonical_prior_weight: float = DEFAULT_CANONICAL_PRIOR_WEIGHT,
    enable_business_concept_retrieval: bool = DEFAULT_ENABLE_BUSINESS_CONCEPT_RETRIEVAL,
    business_concept_weight: float = DEFAULT_BUSINESS_CONCEPT_WEIGHT,
    example_proposed_objects: Sequence[tuple[str, int, str]] = (),
    retrieve_examples: bool = True,
    example_allowed_tiers: frozenset[int] | None = None,
    example_max_examples: int = 3,  # mirrors core.sqlservice.sql_example_retrieval.DEFAULT_MAX_EXAMPLES -- never imported from there, same "stay dependency-free" convention core.sqlservice.sql_generation_service.MAX_FEW_SHOT_EXAMPLES already established
    example_min_similarity: float = 0.15,  # mirrors core.sqlservice.sql_example_retrieval.DEFAULT_MIN_SIMILARITY, same reason
    example_expected_result_shape: str | None = None,
) -> SchemaContextWithExamplesResult:
    """
    The Step 9A internal-only retrieval flow: schema context AND dynamic
    few-shot SQL examples from exactly ONE question embedding.

    1. Runs the identical computation retrieve_schema_context() runs (same
       arguments, same behavior, same SchemaContextResult) via the shared
       private implementation — schema_context is byte-identical whether
       or not any example is ultimately found.
    2. When schema_context.status == STATUS_OK, `retrieve_examples` is True
       (the default), and a query vector was actually produced, reuses
       that SAME vector — never a second provider.embed_batch() call — to
       search this source's trusted example index
       (core.sqlservice.sql_example_retrieval.retrieve_sql_examples),
       against the FULL tier-allowed schema document set for this build
       (not merely the documents this particular question's context
       happened to select) for schema/tier compatibility.
    3. Otherwise, example_retrieval_status is EXAMPLE_RETRIEVAL_NOT_ATTEMPTED
       and examples is empty — example retrieval is never even attempted
       without a real query vector to reuse, and never triggers a second
       embedding call under any circumstance.

    Deliberately a LOCAL (function-body) import of
    core.sqlservice.sql_example_retrieval — a module-level import here
    would cycle: that module's own dependency chain reaches
    core.sqlservice.sql_validation, which imports THIS module (for
    SchemaContextResult/STATUS_OK). Both modules are already fully loaded
    by the time this function is actually CALLED, so the deferred import
    is safe and adds no behavior change.
    """
    schema_context, query_vector = _retrieve_schema_context_with_vector(
        question, build_id, source_id, user_id, provider,
        top_k=top_k, relationship_limit=relationship_limit, max_context_characters=max_context_characters,
        allowed_tiers=allowed_tiers, ranking_mode=ranking_mode, candidate_pool_size=candidate_pool_size,
        max_relationship_path_length=max_relationship_path_length,
        max_projected_columns_per_object=max_projected_columns_per_object,
        max_pool_exempt_neighbors_per_anchor=max_pool_exempt_neighbors_per_anchor,
        max_pool_exempt_neighbors_total=max_pool_exempt_neighbors_total,
        enable_canonical_prior=enable_canonical_prior, canonical_prior_weight=canonical_prior_weight,
        enable_business_concept_retrieval=enable_business_concept_retrieval,
        business_concept_weight=business_concept_weight,
        example_proposed_objects=example_proposed_objects,
    )

    if not retrieve_examples or schema_context.status != STATUS_OK or query_vector is None:
        return SchemaContextWithExamplesResult(
            schema_context=schema_context, examples=(), example_retrieval_status=EXAMPLE_RETRIEVAL_NOT_ATTEMPTED,
        )

    from core.sqlservice.sql_example_retrieval import retrieve_sql_examples

    effective_example_tiers = (
        example_allowed_tiers if example_allowed_tiers is not None
        else (allowed_tiers if allowed_tiers is not None else DEFAULT_ALLOWED_TIERS)
    )

    # A second load_vector_index() call — cheap (in-memory-cached, no
    # SQLite access on a hit; see core.sqlservice.vector_index's own
    # cache), and NOT a second embedding call — the provider is never
    # touched by this function. Reused for the FULL tier-allowed document
    # set (schema/tier compatibility needs every object that exists in
    # this build, not merely the ones this question's own context packed).
    load_result = load_vector_index(
        build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    schema_documents_by_fqn_lower = (
        {d.object_fqn.lower(): d for d in load_result.index.documents} if load_result.index is not None else {}
    )

    example_result = retrieve_sql_examples(
        query_vector, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
        schema_documents_by_fqn_lower,
        allowed_tiers=effective_example_tiers, max_examples=example_max_examples,
        min_similarity=example_min_similarity, expected_result_shape=example_expected_result_shape,
    )

    return SchemaContextWithExamplesResult(
        schema_context=schema_context, examples=example_result.examples,
        example_retrieval_status=example_result.status,
    )
