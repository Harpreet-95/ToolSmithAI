"""
Immutable in-memory vector index over TRUSTED, ACTIVE SQL examples — SQL
rebuild Phase 5, Step 9.

Mirrors core/sqlservice/vector_index.py's design exactly (same normalize-
once/cosine-dot-product-search shape) but loads from
core.sqlservice.sql_example_store / sql_example_embedding_store instead of
the schema-retrieval store, and — critically — filters to TRUST_LEVEL_
TRUSTED and STATUS_ACTIVE examples at INDEX-BUILD time, not merely at some
later filtering step: an untrusted or stale example can never enter this
index, so it structurally cannot be retrieved or reach a prompt regardless
of any bug elsewhere ("Unverified observed queries must not seed tables or
enter SQL prompts").

Scope: never calls an embedding provider, never embeds a question (accepts
an already-computed query vector — see core.sqlservice.
sql_example_retrieval, which reuses the SAME query vector schema-context
retrieval already computed), never connects to a live SQL Server.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import Sequence

from core.sqlservice.sql_example_embedding_store import get_example_embeddings_for_source
from core.sqlservice.sql_example_model import (
    STATUS_ACTIVE,
    TRUST_LEVEL_TRUSTED,
)
from core.sqlservice.sql_example_store import list_examples_for_source

__all__ = [
    "STATUS_OK",
    "STATUS_UNOWNED",
    "STATUS_EMPTY",
    "IndexedSqlExample",
    "SqlExampleVectorIndexIdentity",
    "SqlExampleVectorIndex",
    "SqlExampleSearchResult",
    "SqlExampleVectorIndexLoadResult",
    "load_sql_example_vector_index",
    "search_sql_example_vector_index",
    "clear_sql_example_vector_index_cache",
    "sql_example_vector_index_cache_size",
]

STATUS_OK = "OK"
STATUS_UNOWNED = "UNOWNED"
STATUS_EMPTY = "EMPTY"  # owned source, but zero trusted/active/embedded examples -- not an error


# ── Public data model ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class IndexedSqlExample:
    """One example inside a SqlExampleVectorIndex. `vector` is already
    validated and normalized to unit length at index-build time."""

    example_id: int
    source_id: int
    schema_fingerprint: str
    question_pattern: str
    sql_template: str
    canonical_object_fqns: tuple[str, ...]
    canonical_columns: tuple[str, ...]
    join_path: tuple[str, ...]
    result_shape: str
    trust_level: str
    status: str
    search_document_text: str
    search_document_hash: str
    vector: tuple[float, ...]  # normalized (unit length)


@dataclass(frozen=True)
class SqlExampleVectorIndexIdentity:
    source_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int


@dataclass(frozen=True)
class SqlExampleVectorIndex:
    identity: SqlExampleVectorIndexIdentity
    examples: tuple[IndexedSqlExample, ...]  # deterministic: ordered by example_id


@dataclass(frozen=True)
class SqlExampleSearchResult:
    example: IndexedSqlExample
    score: float


@dataclass(frozen=True)
class SqlExampleVectorIndexLoadResult:
    status: str  # STATUS_OK | STATUS_UNOWNED | STATUS_EMPTY
    identity: SqlExampleVectorIndexIdentity
    index: SqlExampleVectorIndex | None
    example_count: int


# ── Vector validation + normalization (pure, no I/O — deliberately
#    duplicated from core.sqlservice.vector_index's own private helper,
#    same convention core.sqlservice.relationship_graph already
#    established: zero cross-module coupling for a small pure function) ───


def _validate_and_normalize(vector: Sequence[float], expected_dimensions: int) -> tuple[float, ...]:
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


# ── Bounded in-process cache (mirrors vector_index.py's own) ──────────────

_MAX_CACHE_ENTRIES = 16
_index_cache: "OrderedDict[SqlExampleVectorIndexIdentity, SqlExampleVectorIndex]" = OrderedDict()


def clear_sql_example_vector_index_cache() -> None:
    _index_cache.clear()


def sql_example_vector_index_cache_size() -> int:
    return len(_index_cache)


# ── Index loading ──────────────────────────────────────────────────────────


def load_sql_example_vector_index(
    source_id: int,
    user_id: str,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    *,
    use_cache: bool = True,
) -> SqlExampleVectorIndexLoadResult:
    """
    Build (or reuse a cached) immutable index of this source's TRUSTED,
    ACTIVE, already-embedded examples. An example missing an embedding
    under this exact (provider, model, dimensions) identity is simply
    excluded from the index (never a hard failure the way an incomplete
    schema-document build is — examples are an optional enhancement, never
    a gate on question-answering) — a caller wanting to know about
    incomplete example embedding coverage compares
    len(index.examples) against len(list_examples_for_source(...,
    trust_level=TRUST_LEVEL_TRUSTED)) itself.

    Every example in the returned index is guaranteed TRUST_LEVEL_TRUSTED
    and STATUS_ACTIVE by construction (filtered before any vector is even
    looked up) — never a downstream filter's responsibility.
    """
    identity = SqlExampleVectorIndexIdentity(
        source_id=source_id, embedding_provider=embedding_provider,
        embedding_model=embedding_model, embedding_dimensions=embedding_dimensions,
    )

    if use_cache:
        cached = _index_cache.get(identity)
        if cached is not None:
            _index_cache.move_to_end(identity)
            return SqlExampleVectorIndexLoadResult(
                status=STATUS_OK, identity=identity, index=cached, example_count=len(cached.examples),
            )

    trusted_active = list_examples_for_source(
        source_id, user_id, status=STATUS_ACTIVE, trust_level=TRUST_LEVEL_TRUSTED,
    )
    if not trusted_active:
        # Distinguish "unowned" from "owned but empty" the same way every
        # other ownership-scoped list function in this codebase does: an
        # empty result from an ownership-checked query is ambiguous on its
        # own, so re-check ownership directly only when the list came back
        # empty, to report the right status without a second real query
        # for the common (owned, non-empty) case.
        from data.db import get_connection
        conn = get_connection()
        try:
            owned = conn.execute(
                "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?", (source_id, user_id),
            ).fetchone() is not None
        finally:
            conn.close()
        if not owned:
            return SqlExampleVectorIndexLoadResult(status=STATUS_UNOWNED, identity=identity, index=None, example_count=0)
        return SqlExampleVectorIndexLoadResult(status=STATUS_EMPTY, identity=identity, index=None, example_count=0)

    embeddings = get_example_embeddings_for_source(
        source_id, user_id, embedding_provider, embedding_model, embedding_dimensions,
    )
    vector_by_hash = {e.search_document_hash: e.vector for e in embeddings}

    indexed: list[IndexedSqlExample] = []
    for example in trusted_active:
        raw_vector = vector_by_hash.get(example.search_document_hash)
        if raw_vector is None:
            continue  # not yet embedded under this identity -- excluded, not a hard failure
        try:
            normalized = _validate_and_normalize(raw_vector, embedding_dimensions)
        except ValueError:
            continue  # a corrupted stored vector is excluded, never crashes index load
        indexed.append(
            IndexedSqlExample(
                example_id=example.id, source_id=example.source_id, schema_fingerprint=example.schema_fingerprint,
                question_pattern=example.question_pattern, sql_template=example.sql_template,
                canonical_object_fqns=example.canonical_object_fqns, canonical_columns=example.canonical_columns,
                join_path=example.join_path, result_shape=example.result_shape, trust_level=example.trust_level,
                status=example.status, search_document_text=example.search_document_text,
                search_document_hash=example.search_document_hash, vector=normalized,
            )
        )

    if not indexed:
        return SqlExampleVectorIndexLoadResult(status=STATUS_EMPTY, identity=identity, index=None, example_count=0)

    index = SqlExampleVectorIndex(identity=identity, examples=tuple(indexed))
    if use_cache:
        _index_cache[identity] = index
        _index_cache.move_to_end(identity)
        while len(_index_cache) > _MAX_CACHE_ENTRIES:
            _index_cache.popitem(last=False)

    return SqlExampleVectorIndexLoadResult(status=STATUS_OK, identity=identity, index=index, example_count=len(indexed))


# ── Exact cosine search (pure, no I/O) ─────────────────────────────────────


def search_sql_example_vector_index(
    index: SqlExampleVectorIndex,
    query_vector: Sequence[float],
    *,
    top_k: int = 10,
) -> tuple[SqlExampleSearchResult, ...]:
    """
    Exact cosine-similarity search — no SQLite access. Deterministic:
    score descending, example_id ascending tie-break.
    """
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")

    normalized_query = _validate_and_normalize(query_vector, index.identity.embedding_dimensions)

    scored = [
        (ex, sum(q * d for q, d in zip(normalized_query, ex.vector)))
        for ex in index.examples
    ]
    scored.sort(key=lambda pair: (-pair[1], pair[0].example_id))

    return tuple(SqlExampleSearchResult(example=ex, score=score) for ex, score in scored[:top_k])
