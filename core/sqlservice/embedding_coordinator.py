"""
Batched embedding-generation coordinator — SQL rebuild Phase 2, Step 2.

Thin orchestration layer over three existing building blocks — it does not
duplicate any of their logic:

  1. core.sqlservice.retrieval_store.load_stored_retrieval_documents()
     — the READY build's documents, in deterministic (object_fqn) order.
  2. core.sqlservice.embedding_store.get_embedding_build_completeness()
     — the ownership/READY gate, plus which document_hash values already
       have a cached vector for the requested (provider, model, dimensions).
  3. core.sqlservice.embedding_store.insert_embeddings()
     — the one atomic, validated write path into sql_retrieval_embeddings.

This module's own job is exactly what's between those: deduplicate
documents down to their distinct missing hashes, plan bounded batches,
call an injected EmbeddingProvider (core.sqlservice.embedding_providers),
validate every response before trusting it, and only call insert_embeddings()
once every batch has succeeded — never partially.

Scope: never calls a real embedding provider itself (that's
EmbeddingProvider's job, injected by the caller), never persists anything
outside insert_embeddings(), never runs schema discovery or connects to a
live SQL Server, and never performs vector similarity search.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from core.config import SQL_EMBEDDING_MAX_BATCH_CHARS, SQL_EMBEDDING_MAX_BATCH_ITEMS
from core.sqlservice.embedding_providers import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
    ProviderEmbeddingBatchResponse,
)
from core.sqlservice.embedding_store import (
    STATUS_OK,
    get_embedding_build_completeness,
    insert_embeddings,
)
from core.sqlservice.retrieval_store import load_stored_retrieval_documents

__all__ = [
    "EmbeddingGenerationError",
    "EmbeddingConfigurationError",
    "EmbeddingProviderError",
    "EmbeddingValidationError",
    "EmbeddingPersistenceError",
    "EmbeddingGenerationResult",
    "generate_missing_embeddings",
]


# ── Typed failure hierarchy ────────────────────────────────────────────────
#
# Ownership/build-state conditions are reported via `status` on the result
# (see EmbeddingGenerationResult) — a routine, expected condition, never
# raised. Everything else this module can fail on is one of the four
# exception types below, so a caller can `except` the exact category it
# cares about instead of a bare Exception.


class EmbeddingGenerationError(Exception):
    """Base class for every exception generate_missing_embeddings() itself
    raises (re-exported EmbeddingConfigurationError/EmbeddingProviderError
    from embedding_providers are NOT subclasses of this — they're raised
    as-is, unwrapped, so a caller catching those two directly still works)."""


class EmbeddingValidationError(EmbeddingGenerationError):
    """A document exceeded the configured size limit, or a provider's
    response failed validation (wrong count, bad indices, wrong dimensions,
    non-finite values) — the request or response shape was wrong, not the
    provider call itself."""


class EmbeddingPersistenceError(EmbeddingGenerationError):
    """insert_embeddings() raised after every batch was already validated —
    e.g. a concurrent conflicting vector. Every batch's vectors are
    validated in memory before this call is ever made; this exception means
    persistence itself failed, not validation."""


# ── Public result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class EmbeddingGenerationResult:
    """
    Result of one generate_missing_embeddings() call.

    total_documents: distinct document_hash values in the build.
    requested_count: distinct missing hashes actually sent to the provider
        this call (0 when everything was already cached — no API call made).
    generated_count: hashes newly written to the cache this call.
    reused_count: hashes that already had a cached vector — either before
        this call started, or (rarely) inserted by a concurrent caller
        between this call's completeness check and its own insert.
    """

    status: str  # STATUS_OK | STATUS_UNOWNED | STATUS_BUILD_NOT_FOUND | STATUS_BUILD_NOT_READY
    build_id: int
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    total_documents: int
    requested_count: int
    generated_count: int
    reused_count: int


def _empty_result(status: str, build_id: int, provider: EmbeddingProvider) -> EmbeddingGenerationResult:
    return EmbeddingGenerationResult(
        status=status,
        build_id=build_id,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        total_documents=0,
        requested_count=0,
        generated_count=0,
        reused_count=0,
    )


# ── Batch planning (pure, no I/O) ──────────────────────────────────────────


def _plan_batches(
    text_by_hash: dict[str, str], max_batch_items: int, max_batch_chars: int,
) -> list[list[tuple[str, str]]]:
    """
    Greedily pack (document_hash, document_text) pairs, in the order
    `text_by_hash` iterates, into batches that respect both max_batch_items
    and max_batch_chars. A document whose own length exceeds
    max_batch_chars can never fit any batch, so it is rejected immediately
    with a clear error rather than silently starting a batch of one that
    would still be too large.
    """
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_chars = 0

    for document_hash, text in text_by_hash.items():
        text_len = len(text)
        if text_len > max_batch_chars:
            raise EmbeddingValidationError(
                f"document_hash={document_hash!r} has {text_len} characters, exceeding "
                f"max_batch_chars={max_batch_chars}; it can never fit in any batch"
            )

        would_overflow_items = len(current) + 1 > max_batch_items
        would_overflow_chars = current_chars + text_len > max_batch_chars
        if current and (would_overflow_items or would_overflow_chars):
            batches.append(current)
            current = []
            current_chars = 0

        current.append((document_hash, text))
        current_chars += text_len

    if current:
        batches.append(current)
    return batches


# ── Response validation (pure, no I/O) ────────────────────────────────────


def _validate_finite_vector(vector: Sequence[float]) -> None:
    for i, value in enumerate(vector):
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise EmbeddingValidationError(f"vector element {i} is not a number: {value!r}") from exc
        if math.isnan(f) or math.isinf(f):
            raise EmbeddingValidationError(f"vector element {i} is not finite: {f!r}")


def _validate_and_reorder_batch(
    response: ProviderEmbeddingBatchResponse,
    batch: list[tuple[str, str]],
    expected_dimensions: int,
) -> dict[str, tuple[float, ...]]:
    """
    Validate one provider response against the batch that produced it, and
    return {document_hash: vector} realigned to the batch's own order —
    never the provider's response order, which is not guaranteed to match
    request order (see the `index` field on each item).
    """
    expected_count = len(batch)
    items = response.items

    if len(items) != expected_count:
        raise EmbeddingValidationError(
            f"provider returned {len(items)} embeddings for a batch of {expected_count} documents"
        )

    by_index: dict[int, tuple[float, ...]] = {}
    for item in items:
        index = item.index
        if not isinstance(index, int) or isinstance(index, bool):
            raise EmbeddingValidationError(f"provider returned a non-integer index: {index!r}")
        if index < 0 or index >= expected_count:
            raise EmbeddingValidationError(
                f"provider returned out-of-range index {index} for a batch of {expected_count} documents"
            )
        if index in by_index:
            raise EmbeddingValidationError(f"provider returned duplicate index {index}")
        by_index[index] = item.vector

    missing = sorted(set(range(expected_count)) - by_index.keys())
    if missing:
        raise EmbeddingValidationError(f"provider response is missing indices: {missing}")

    result: dict[str, tuple[float, ...]] = {}
    for position, (document_hash, _text) in enumerate(batch):
        vector = by_index[position]
        if len(vector) != expected_dimensions:
            raise EmbeddingValidationError(
                f"document_hash={document_hash!r} embedding has {len(vector)} dimensions, "
                f"expected {expected_dimensions}"
            )
        _validate_finite_vector(vector)
        result[document_hash] = tuple(float(v) for v in vector)
    return result


# ── Public entry point ────────────────────────────────────────────────────


def generate_missing_embeddings(
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    max_batch_items: int = SQL_EMBEDDING_MAX_BATCH_ITEMS,
    max_batch_chars: int = SQL_EMBEDDING_MAX_BATCH_CHARS,
) -> EmbeddingGenerationResult:
    """
    Generate and persist embeddings for every document in one owned, READY
    build that doesn't already have one cached under `provider`'s exact
    (provider_id, model, dimensions) identity.

    Sequence:
      1. Validate max_batch_items/max_batch_chars are positive — raises
         EmbeddingConfigurationError otherwise (a caller/deployment
         misconfiguration, not a provider or data problem).
      2. get_embedding_build_completeness() — the one ownership/READY gate
         plus the missing-hash set. A non-OK status short-circuits here
         with zero counts and no provider call at all.
      3. If nothing is missing, return immediately — STATUS_OK, all counts
         accounted for as `reused_count`, zero provider calls.
      4. load_stored_retrieval_documents() — every document in the build,
         in deterministic object_fqn order — filtered down to the missing
         hashes and deduplicated by hash (two objects can share identical
         document_text, hence the same document_hash; only one copy of
         that text is ever sent to the provider).
      5. Plan bounded batches (see _plan_batches) and call
         provider.embed_batch() once per batch, in order. Each response is
         validated and reordered to the batch's own order (see
         _validate_and_reorder_batch) before being accumulated into one
         in-memory {document_hash: vector} map.
      6. Only after every batch succeeds: one atomic insert_embeddings()
         call for the entire accumulated map. Any failure in steps 5-6 —
         a provider exception, a malformed response, a persistence error —
         means NOTHING from this call is written, including batches that
         individually validated fine earlier in the same call.

    Raises:
        EmbeddingConfigurationError: invalid batch-size configuration, or
            propagated unchanged from the provider (e.g. missing API key).
        EmbeddingProviderError: propagated unchanged from the provider
            (the API call itself failed).
        EmbeddingValidationError: an oversized document, or a malformed
            provider response.
        EmbeddingPersistenceError: insert_embeddings() failed after every
            batch was already validated.
    """
    if not isinstance(max_batch_items, int) or isinstance(max_batch_items, bool) or max_batch_items <= 0:
        raise EmbeddingConfigurationError(f"max_batch_items must be a positive integer, got {max_batch_items!r}")
    if not isinstance(max_batch_chars, int) or isinstance(max_batch_chars, bool) or max_batch_chars <= 0:
        raise EmbeddingConfigurationError(f"max_batch_chars must be a positive integer, got {max_batch_chars!r}")

    completeness = get_embedding_build_completeness(
        build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    if completeness.status != STATUS_OK:
        return _empty_result(completeness.status, build_id, provider)

    total_documents = completeness.total_documents
    missing_hashes = set(completeness.missing_document_hashes)

    if not missing_hashes:
        return EmbeddingGenerationResult(
            status=STATUS_OK,
            build_id=build_id,
            embedding_provider=provider.provider_id,
            embedding_model=provider.model,
            embedding_dimensions=provider.dimensions,
            total_documents=total_documents,
            requested_count=0,
            generated_count=0,
            reused_count=total_documents,
        )

    stored_documents = load_stored_retrieval_documents(build_id, source_id, user_id)

    # Deterministic + deduplicated: iterate in object_fqn order (as
    # returned), keep only missing hashes, and let dict insertion collapse
    # two documents that share an identical document_hash (same text, by
    # definition of a content hash) down to one entry.
    text_by_hash: dict[str, str] = {}
    for document in stored_documents:
        if document.document_hash in missing_hashes and document.document_hash not in text_by_hash:
            text_by_hash[document.document_hash] = document.document_text

    batches = _plan_batches(text_by_hash, max_batch_items, max_batch_chars)

    accumulated_vectors: dict[str, tuple[float, ...]] = {}
    for batch in batches:
        texts = [text for _document_hash, text in batch]
        try:
            response = provider.embed_batch(texts)
        except (EmbeddingConfigurationError, EmbeddingProviderError):
            raise  # propagate unchanged — never wrapped, never partially persisted
        except Exception as exc:  # noqa: BLE001 - a misbehaving provider that raises something else is still a provider failure
            raise EmbeddingProviderError(f"{type(exc).__name__}: {exc}") from exc

        accumulated_vectors.update(
            _validate_and_reorder_batch(response, batch, provider.dimensions)
        )

    try:
        insert_result = insert_embeddings(
            build_id, source_id, user_id,
            provider.provider_id, provider.model, provider.dimensions,
            accumulated_vectors,
        )
    except Exception as exc:
        raise EmbeddingPersistenceError(f"{type(exc).__name__}: {exc}") from exc

    if insert_result.status != STATUS_OK:
        return _empty_result(insert_result.status, build_id, provider)

    requested_count = len(text_by_hash)
    generated_count = len(insert_result.inserted_hashes)
    reused_count = (total_documents - requested_count) + len(insert_result.reused_hashes)

    return EmbeddingGenerationResult(
        status=STATUS_OK,
        build_id=build_id,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        total_documents=total_documents,
        requested_count=requested_count,
        generated_count=generated_count,
        reused_count=reused_count,
    )
