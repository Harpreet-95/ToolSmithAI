"""
Immutable in-memory vector index and exact cosine search — SQL rebuild
Phase 2, Step 3.

Thin orchestration layer over two existing building blocks — it does not
duplicate any of their logic:

  1. core.sqlservice.embedding_store.get_embedding_build_completeness()
     — the ownership/READY gate, plus whether every document in the build
       already has a cached vector for the requested (provider, model,
       dimensions) identity.
  2. core.sqlservice.retrieval_store.load_stored_retrieval_documents() and
     core.sqlservice.embedding_store.get_embeddings_for_build()
     — the build's documents and their cached vectors, both already in
       deterministic object_fqn order.

This module's own job is exactly what's between those: validate and
normalize every stored vector once, build an immutable VectorIndex, cache
it (bounded, keyed by exact identity), and search it with the SQLite
connection long since closed — no I/O of any kind happens during a search.

Scope: never calls OpenAI or any other embedding provider, never embeds a
natural-language question (that is a later step's job — this module only
accepts an already-computed query vector), never connects to a live SQL
Server, and never performs schema discovery or SQL generation.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping, Sequence

from core.sqlservice.embedding_store import (
    STATUS_BUILD_NOT_FOUND,
    STATUS_BUILD_NOT_READY,
    STATUS_OK,
    STATUS_UNOWNED,
    get_embedding_build_completeness,
    get_embeddings_for_build,
)
from core.sqlservice.retrieval_store import load_stored_retrieval_documents

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "STATUS_BUILD_NOT_FOUND",
    "STATUS_BUILD_NOT_READY",
    "STATUS_INCOMPLETE",
    "STATUS_INVALID",
    "IndexedSchemaDocument",
    "VectorIndexIdentity",
    "VectorIndex",
    "VectorSearchResult",
    "VectorIndexLoadResult",
    "load_vector_index",
    "search_vector_index",
    "clear_vector_index_cache",
    "vector_index_cache_size",
]

# Additional statuses beyond the four embedding_store gate statuses
# re-exported above — specific to this module's own build gate (item 2 in
# the module docstring): valid, complete data is a precondition for
# building an index, not merely for reusing a cached embedding.
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_INVALID = "INVALID"

_ALLOWED_TIERS = frozenset({1, 2, 3, 4})


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexedSchemaDocument:
    """One document inside a VectorIndex. `vector` is already validated and
    normalized to unit length at index-build time — never the raw stored
    vector — so search-time scoring is a plain dot product, nothing more.
    `metadata` is the same deeply-immutable mapping StoredRetrievalDocument
    already carries (see core/sqlservice/retrieval_store.py); this module
    does not re-wrap or re-validate it."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]
    vector: tuple[float, ...]  # normalized (unit length)


@dataclass(frozen=True)
class VectorIndexIdentity:
    """Exact cache/index identity — build_id, provider, model, and
    dimensions together. Two identities that differ in any one field are
    never considered the same index, and a VectorIndex built under one
    identity is never returned for another."""

    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class VectorIndex:
    """An immutable, ready-to-search index: every document normalized and
    validated once at load time. `documents` is in deterministic object_fqn
    order (inherited from load_stored_retrieval_documents())."""

    identity: VectorIndexIdentity
    documents: tuple[IndexedSchemaDocument, ...]


@dataclass(frozen=True)
class VectorSearchResult:
    """One ranked hit from search_vector_index()."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]
    score: float


@dataclass(frozen=True)
class VectorIndexLoadResult:
    """
    Result of one load_vector_index() call.

    status: STATUS_OK | STATUS_UNOWNED | STATUS_BUILD_NOT_FOUND |
        STATUS_BUILD_NOT_READY | STATUS_INCOMPLETE | STATUS_INVALID
    index: the built (or cached) VectorIndex — only present when status is
        STATUS_OK; None otherwise. A caller must check `status` before ever
        touching `index`.
    document_count: total documents accounted for at whichever stage
        `status` reflects (0 for a gate failure; the build's total
        document count for STATUS_INCOMPLETE/STATUS_INVALID; the actual
        indexed count for STATUS_OK).
    error: a human-readable detail for STATUS_INCOMPLETE/STATUS_INVALID —
        always None for every other status.
    """

    status: str
    identity: VectorIndexIdentity
    index: VectorIndex | None
    document_count: int
    error: str | None


# ── Vector validation + normalization (pure, no I/O) ──────────────────────


def _validate_and_normalize(vector: Sequence[float], expected_dimensions: int) -> tuple[float, ...]:
    """
    Shared by both a stored vector (at index-build time) and a query vector
    (at search time). Raises ValueError for:
      - an empty vector
      - a length not equal to expected_dimensions
      - a non-numeric element
      - a non-finite element (NaN or +/-Infinity)
      - a zero-norm vector (cannot be normalized to unit length)
    """
    try:
        length = len(vector)
    except TypeError as exc:
        raise ValueError("vector must be a sized sequence of numbers") from exc
    if length == 0:
        raise ValueError("vector must not be empty")
    if length != expected_dimensions:
        raise ValueError(f"vector has {length} dimensions, expected {expected_dimensions}")

    floats: list[float] = []
    for i, raw in enumerate(vector):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"vector element {i} is not a number: {raw!r}") from exc
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"vector element {i} is not finite: {value!r}")
        floats.append(value)

    norm = math.sqrt(sum(f * f for f in floats))
    if norm == 0.0:
        raise ValueError("vector has zero norm and cannot be normalized")

    return tuple(f / norm for f in floats)


# ── Bounded in-process cache ───────────────────────────────────────────────
#
# Keyed on the exact VectorIndexIdentity (build_id, provider, model,
# dimensions) — a frozen dataclass of hashable fields, usable directly as a
# dict key. Bounded LRU: the least-recently-used entry is evicted once the
# cache would exceed _MAX_CACHE_ENTRIES, so a long-running process never
# accumulates unbounded memory across many builds/identities.

_MAX_CACHE_ENTRIES = 16
_index_cache: "OrderedDict[VectorIndexIdentity, VectorIndex]" = OrderedDict()


def _get_cached(identity: VectorIndexIdentity) -> VectorIndex | None:
    index = _index_cache.get(identity)
    if index is not None:
        _index_cache.move_to_end(identity)
    return index


def _put_cached(identity: VectorIndexIdentity, index: VectorIndex) -> None:
    _index_cache[identity] = index
    _index_cache.move_to_end(identity)
    while len(_index_cache) > _MAX_CACHE_ENTRIES:
        _index_cache.popitem(last=False)


def clear_vector_index_cache() -> None:
    """
    Explicitly evict every cached VectorIndex. For tests (isolating one
    test's cache state from another's) and for future rebuild invalidation
    (e.g. after re-embedding a build, so a stale index is never served
    again under its old identity).
    """
    _index_cache.clear()


def vector_index_cache_size() -> int:
    """Number of identities currently cached — exposed for tests."""
    return len(_index_cache)


# ── Index loading (the only function in this module that touches SQLite) ──


def load_vector_index(
    build_id: int,
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    *,
    use_cache: bool = True,
) -> VectorIndexLoadResult:
    """
    Build (or reuse a cached) immutable VectorIndex for one owned, READY
    build whose embeddings are complete under the exact (embedding_provider,
    embedding_model, embedding_dimensions) identity.

    The ownership/READY/completeness gate (via
    get_embedding_build_completeness()) is checked on EVERY call, cache hit
    or not — the cache only ever skips the expensive part (decoding and
    normalizing every stored vector, and re-walking every document), never
    the gate itself. A non-owner can therefore never obtain a cached index
    for a build they don't own, regardless of who built it first.

    Fails closed: if any single stored vector is malformed, or any document
    has no matching cached embedding despite the build reporting complete
    (an unexpected state, not a normal race this function retries), the
    WHOLE index is refused — STATUS_INVALID, index=None — never a partial
    index silently missing one document.
    """
    identity = VectorIndexIdentity(
        build_id=build_id,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions,
    )

    completeness = get_embedding_build_completeness(
        build_id, source_id, user_id, embedding_provider, embedding_model, embedding_dimensions,
    )
    if completeness.status != STATUS_OK:
        return VectorIndexLoadResult(
            status=completeness.status, identity=identity, index=None, document_count=0, error=None,
        )
    if not completeness.is_complete:
        return VectorIndexLoadResult(
            status=STATUS_INCOMPLETE,
            identity=identity,
            index=None,
            document_count=completeness.total_documents,
            error=(
                f"{len(completeness.missing_document_hashes)} of "
                f"{completeness.total_documents} documents are missing an embedding "
                f"for this provider/model/dimensions"
            ),
        )

    if use_cache:
        cached = _get_cached(identity)
        if cached is not None:
            return VectorIndexLoadResult(
                status=STATUS_OK, identity=identity, index=cached,
                document_count=len(cached.documents), error=None,
            )

    stored_documents = load_stored_retrieval_documents(build_id, source_id, user_id)
    stored_embeddings = get_embeddings_for_build(
        build_id, source_id, user_id, embedding_provider, embedding_model, embedding_dimensions,
    )
    vectors_by_hash = {e.document_hash: e.vector for e in stored_embeddings}

    indexed: list[IndexedSchemaDocument] = []
    for document in stored_documents:
        raw_vector = vectors_by_hash.get(document.document_hash)
        if raw_vector is None:
            return VectorIndexLoadResult(
                status=STATUS_INVALID,
                identity=identity,
                index=None,
                document_count=len(stored_documents),
                error=(
                    f"document_hash={document.document_hash!r} (object_fqn={document.object_fqn!r}) "
                    f"has no matching cached embedding despite a complete build"
                ),
            )
        try:
            normalized = _validate_and_normalize(raw_vector, embedding_dimensions)
        except ValueError as exc:
            return VectorIndexLoadResult(
                status=STATUS_INVALID,
                identity=identity,
                index=None,
                document_count=len(stored_documents),
                error=f"document_hash={document.document_hash!r} (object_fqn={document.object_fqn!r}): {exc}",
            )
        indexed.append(
            IndexedSchemaDocument(
                object_fqn=document.object_fqn,
                object_type=document.object_type,
                retrieval_tier=document.retrieval_tier,
                document_hash=document.document_hash,
                document_text=document.document_text,
                metadata=document.metadata,
                vector=normalized,
            )
        )

    index = VectorIndex(identity=identity, documents=tuple(indexed))
    if use_cache:
        _put_cached(identity, index)

    return VectorIndexLoadResult(
        status=STATUS_OK, identity=identity, index=index, document_count=len(indexed), error=None,
    )


# ── Exact cosine search (pure, no I/O) ─────────────────────────────────────


def search_vector_index(
    index: VectorIndex,
    query_vector: Sequence[float],
    *,
    top_k: int = 10,
    tier: int | None = None,
) -> tuple[VectorSearchResult, ...]:
    """
    Exact cosine-similarity search over an already-loaded VectorIndex — no
    SQLite access of any kind. The query vector is validated and normalized
    exactly once (see _validate_and_normalize); every stored vector was
    already normalized when the index was built, so each score is a single
    dot product of two unit vectors — no per-document norm computation, no
    re-decoding, no re-copying.

    Ranking is by score descending, with object_fqn ascending as a
    deterministic tie-break — two calls with the same index and query
    vector always return the identical ordered result, and a tie in score
    never depends on dict/insertion order.

    Returns at most `top_k` results — fewer when the (optionally
    tier-filtered) candidate set is smaller; never an error for that case.

    Raises:
        ValueError: an invalid query vector (empty, wrong dimension,
            non-finite element, zero norm), a non-positive top_k, or a
            tier outside {1, 2, 3, 4}.
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
    if tier is not None and tier not in _ALLOWED_TIERS:
        raise ValueError(f"tier must be one of {sorted(_ALLOWED_TIERS)} or None, got {tier!r}")

    normalized_query = _validate_and_normalize(query_vector, index.identity.embedding_dimensions)

    candidates = index.documents if tier is None else tuple(
        doc for doc in index.documents if doc.retrieval_tier == tier
    )

    scored = [
        (doc, sum(q * d for q, d in zip(normalized_query, doc.vector)))
        for doc in candidates
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].object_fqn))

    return tuple(
        VectorSearchResult(
            object_fqn=doc.object_fqn,
            object_type=doc.object_type,
            retrieval_tier=doc.retrieval_tier,
            document_hash=doc.document_hash,
            document_text=doc.document_text,
            metadata=doc.metadata,
            score=score,
        )
        for doc, score in scored[:top_k]
    )
