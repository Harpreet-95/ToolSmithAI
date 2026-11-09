"""
Connection-time schema-retrieval preparation service — SQL rebuild
Phase 2, Step 5.

One typed orchestration function over three existing, already-tested
services — it does not duplicate any of their logic:

  1. core.sqlservice.retrieval_coordinator.build_and_persist_retrieval_documents()
     — schema snapshot -> retrieval documents -> snapshot-safe fingerprint
       -> idempotent persistence (reuses an unchanged build, creates a new
       one on real schema drift).
  2. core.sqlservice.embedding_coordinator.generate_missing_embeddings()
     — content-addressed embedding generation; an unchanged document_hash
       is reused across builds automatically (see core/sqlservice/
       embedding_store.py), so this stage only ever calls the provider for
       genuinely new or changed documents.
  3. core.sqlservice.vector_index.load_vector_index()
     — the bounded, identity-keyed cached immutable index, ready to search.

This module's own job is exactly the sequencing between those three: run
them in order, short-circuit on the first failure (never call a later
stage after an earlier one failed), time each stage, and assemble one
immutable result a connection-time caller can act on without needing to
know any of the three services' own individual return shapes.

Scope: never calls a real embedding provider itself (injected by the
caller), never generates or executes SQL, never connects to a live SQL
Server, and is not wired into any route or the connection-time
coordinator — this module is the missing piece THAT wiring will later
call, not the wiring itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from core.sqlservice.embedding_coordinator import generate_missing_embeddings
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.embedding_store import STATUS_OK as _EMBEDDING_STATUS_OK
from core.sqlservice.retrieval_coordinator import (
    STATUS_CREATED as _COORDINATOR_STATUS_CREATED,
)
from core.sqlservice.retrieval_coordinator import (
    STATUS_REUSED as _COORDINATOR_STATUS_REUSED,
)
from core.sqlservice.retrieval_coordinator import build_and_persist_retrieval_documents
from core.sqlservice.vector_index import STATUS_OK as _VECTOR_INDEX_STATUS_OK
from core.sqlservice.vector_index import load_vector_index

__all__ = [
    "STATUS_OK",
    "STATUS_DOCUMENT_BUILD_FAILED",
    "STATUS_EMBEDDING_FAILED",
    "STATUS_VECTOR_INDEX_FAILED",
    "BUILD_OUTCOME_CREATED",
    "BUILD_OUTCOME_REUSED",
    "RetrievalPreparationResult",
    "prepare_source_for_retrieval",
]

# ── Status vocabulary ──────────────────────────────────────────────────────
#
# One combined namespace spanning all three stages, each with its OWN
# status vocabulary (retrieval_coordinator's READY_CREATED/READY_REUSED/...,
# embedding_store's OK/UNOWNED/..., vector_index's OK/UNOWNED/.../INVALID).
# This module never invents a finer-grained outer status per underlying
# sub-status — it only needs to say WHICH STAGE failed; the underlying
# sub-status and message are preserved verbatim in `error` (see
# requirement: "retaining safe diagnostic information").

STATUS_OK = "OK"
STATUS_DOCUMENT_BUILD_FAILED = "DOCUMENT_BUILD_FAILED"
STATUS_EMBEDDING_FAILED = "EMBEDDING_FAILED"
STATUS_VECTOR_INDEX_FAILED = "VECTOR_INDEX_FAILED"

BUILD_OUTCOME_CREATED = "CREATED"
BUILD_OUTCOME_REUSED = "REUSED"


# ── Public result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetrievalPreparationResult:
    """
    Result of one prepare_source_for_retrieval() call.

    status: STATUS_OK | STATUS_DOCUMENT_BUILD_FAILED | STATUS_EMBEDDING_FAILED
        | STATUS_VECTOR_INDEX_FAILED. Only STATUS_OK means the vector index
        is actually ready to search — every other status means preparation
        stopped at that stage and every field describing a LATER stage is
        left at its not-yet-reached default (0 counts, None identity).
    build_outcome: BUILD_OUTCOME_CREATED | BUILD_OUTCOME_REUSED | None (the
        latter only when the document-build stage itself never completed).
    embeddings_generated / embeddings_reused: from the embedding stage's own
        EmbeddingGenerationResult — 0/0 whenever that stage never ran.
    vector_index_document_count: 0 whenever the index stage never ran or
        never completed.
    *_duration_seconds: measured with the (injectable) `clock` — 0.0 for a
        stage that never ran. total_duration_seconds always reflects
        everything actually executed, success or failure.
    """

    status: str
    source_id: int
    build_id: int | None
    snapshot_id: int | None
    snapshot_version: int | None
    schema_fingerprint: str | None
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    build_outcome: str | None
    total_document_count: int
    embeddings_generated: int
    embeddings_reused: int
    vector_index_document_count: int
    warnings: tuple[str, ...]
    error: str | None
    document_build_duration_seconds: float
    embedding_duration_seconds: float
    index_load_duration_seconds: float
    total_duration_seconds: float


# ── Result constructors (keep the main function readable) ────────────────


def _document_build_failed(
    source_id: int, provider: EmbeddingProvider, coordination_result,
    document_build_duration: float, total_duration: float,
) -> RetrievalPreparationResult:
    return RetrievalPreparationResult(
        status=STATUS_DOCUMENT_BUILD_FAILED,
        source_id=source_id,
        build_id=coordination_result.build_id,
        snapshot_id=coordination_result.snapshot_id,
        snapshot_version=coordination_result.snapshot_version,
        schema_fingerprint=coordination_result.schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        build_outcome=None,
        total_document_count=0,
        embeddings_generated=0,
        embeddings_reused=0,
        vector_index_document_count=0,
        warnings=coordination_result.warnings,
        error=(
            f"retrieval document build failed: status={coordination_result.status!r} "
            f"error={coordination_result.error!r}"
        ),
        document_build_duration_seconds=document_build_duration,
        embedding_duration_seconds=0.0,
        index_load_duration_seconds=0.0,
        total_duration_seconds=total_duration,
    )


def _embedding_failed(
    source_id: int, provider: EmbeddingProvider, coordination_result, build_outcome: str,
    embedding_result, document_build_duration: float, embedding_duration: float, total_duration: float,
) -> RetrievalPreparationResult:
    return RetrievalPreparationResult(
        status=STATUS_EMBEDDING_FAILED,
        source_id=source_id,
        build_id=coordination_result.build_id,
        snapshot_id=coordination_result.snapshot_id,
        snapshot_version=coordination_result.snapshot_version,
        schema_fingerprint=coordination_result.schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        build_outcome=build_outcome,
        total_document_count=coordination_result.document_count,
        embeddings_generated=0,
        embeddings_reused=0,
        vector_index_document_count=0,
        warnings=coordination_result.warnings,
        error=f"embedding generation failed: status={embedding_result.status!r}",
        document_build_duration_seconds=document_build_duration,
        embedding_duration_seconds=embedding_duration,
        index_load_duration_seconds=0.0,
        total_duration_seconds=total_duration,
    )


def _vector_index_failed(
    source_id: int, provider: EmbeddingProvider, coordination_result, build_outcome: str,
    embedding_result, index_result, document_build_duration: float, embedding_duration: float,
    index_load_duration: float, total_duration: float,
) -> RetrievalPreparationResult:
    return RetrievalPreparationResult(
        status=STATUS_VECTOR_INDEX_FAILED,
        source_id=source_id,
        build_id=coordination_result.build_id,
        snapshot_id=coordination_result.snapshot_id,
        snapshot_version=coordination_result.snapshot_version,
        schema_fingerprint=coordination_result.schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        build_outcome=build_outcome,
        total_document_count=coordination_result.document_count,
        embeddings_generated=embedding_result.generated_count,
        embeddings_reused=embedding_result.reused_count,
        vector_index_document_count=0,
        warnings=coordination_result.warnings,
        error=f"vector index load failed: status={index_result.status!r} error={index_result.error!r}",
        document_build_duration_seconds=document_build_duration,
        embedding_duration_seconds=embedding_duration,
        index_load_duration_seconds=index_load_duration,
        total_duration_seconds=total_duration,
    )


# ── Public entry point ────────────────────────────────────────────────────


def prepare_source_for_retrieval(
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    max_batch_items: int | None = None,
    max_batch_chars: int | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> RetrievalPreparationResult:
    """
    Prepare one source end to end: retrieval-document build/persist ->
    missing-embedding generation -> vector-index load.

    Runs the three stages in that exact order and short-circuits on the
    first one that doesn't succeed — a later stage is never invoked after
    an earlier one failed, so e.g. no provider call is ever made when the
    document build itself failed.

    Does NOT catch or wrap exceptions: EmbeddingConfigurationError,
    EmbeddingProviderError, EmbeddingValidationError, EmbeddingPersistenceError
    (from generate_missing_embeddings) and any ValueError from malformed
    input propagate completely unchanged, exactly as they would from
    calling that stage directly — these are genuine exceptional conditions,
    not "expected" ones. Only each stage's own EXPECTED, routine typed
    status (ownership, build-not-found, build-not-ready, incomplete
    embeddings, invalid stored data, snapshot/fingerprint races that
    exhausted their retry budget, ...) is converted into this function's
    own STATUS_DOCUMENT_BUILD_FAILED / STATUS_EMBEDDING_FAILED /
    STATUS_VECTOR_INDEX_FAILED, with the underlying stage's own status and
    message preserved verbatim in `error` — never swallowed, never turned
    into a generic "something went wrong".

    Never reads or exposes provider credentials: every field on the
    returned result comes from provider.provider_id/model/dimensions (all
    non-secret identity, by EmbeddingProvider's own contract) or from the
    three sub-services' own results, none of which carry credentials.

    `clock` defaults to time.perf_counter but is fully injectable — pass a
    deterministic callable (e.g. a fixed-step counter) for reproducible
    timing assertions in tests, instead of monkeypatching time.perf_counter
    globally.

    `max_batch_items`/`max_batch_chars`, when given, are forwarded to
    generate_missing_embeddings() unchanged; None (the default) lets that
    function use its own configured defaults — this module never invents
    its own default batch-size values.
    """
    total_start = clock()

    stage_start = clock()
    coordination_result = build_and_persist_retrieval_documents(source_id, user_id)
    document_build_duration = clock() - stage_start

    if coordination_result.status not in (_COORDINATOR_STATUS_CREATED, _COORDINATOR_STATUS_REUSED):
        return _document_build_failed(
            source_id, provider, coordination_result,
            document_build_duration, clock() - total_start,
        )

    build_outcome = (
        BUILD_OUTCOME_CREATED if coordination_result.status == _COORDINATOR_STATUS_CREATED
        else BUILD_OUTCOME_REUSED
    )
    build_id = coordination_result.build_id

    embedding_kwargs = {}
    if max_batch_items is not None:
        embedding_kwargs["max_batch_items"] = max_batch_items
    if max_batch_chars is not None:
        embedding_kwargs["max_batch_chars"] = max_batch_chars

    stage_start = clock()
    embedding_result = generate_missing_embeddings(build_id, source_id, user_id, provider, **embedding_kwargs)
    embedding_duration = clock() - stage_start

    if embedding_result.status != _EMBEDDING_STATUS_OK:
        return _embedding_failed(
            source_id, provider, coordination_result, build_outcome, embedding_result,
            document_build_duration, embedding_duration, clock() - total_start,
        )

    stage_start = clock()
    index_result = load_vector_index(
        build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    index_load_duration = clock() - stage_start

    if index_result.status != _VECTOR_INDEX_STATUS_OK:
        return _vector_index_failed(
            source_id, provider, coordination_result, build_outcome, embedding_result, index_result,
            document_build_duration, embedding_duration, index_load_duration, clock() - total_start,
        )

    return RetrievalPreparationResult(
        status=STATUS_OK,
        source_id=source_id,
        build_id=build_id,
        snapshot_id=coordination_result.snapshot_id,
        snapshot_version=coordination_result.snapshot_version,
        schema_fingerprint=coordination_result.schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        build_outcome=build_outcome,
        total_document_count=coordination_result.document_count,
        embeddings_generated=embedding_result.generated_count,
        embeddings_reused=embedding_result.reused_count,
        vector_index_document_count=index_result.document_count,
        warnings=coordination_result.warnings,
        error=None,
        document_build_duration_seconds=document_build_duration,
        embedding_duration_seconds=embedding_duration,
        index_load_duration_seconds=index_load_duration,
        total_duration_seconds=clock() - total_start,
    )
