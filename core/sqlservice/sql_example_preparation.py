"""
Bounded automatic SQL-example preparation coordinator — SQL rebuild
Phase 5, Step 11B.

One typed orchestration function over existing, already-tested building
blocks — it does not duplicate any of their logic:

  1. core.sqlservice.retrieval_store.get_latest_ready_retrieval_build() /
     load_stored_retrieval_documents()
     — the ownership/READY gate for the source's latest schema build, and
       its persisted documents.
  2. core.sqlservice.sql_example_generator.generate_trusted_examples()
     — the deterministic, schema-only, zero-I/O Step 11A generator; every
       candidate it returns has already cleared the real structural-
       validation and trust-classification gates (see that module's own
       docstring).
  3. core.sqlservice.sql_example_store.insert_trusted_example()
     — the ONLY way to persist a TRUSTED row; idempotent per
       (source_id, content_hash).
  4. core.sqlservice.sql_example_embedding_store.
     find_missing_example_search_document_hashes() / insert_example_embeddings()
     — the content-addressed embedding cache for search-document text.
  5. core.sqlservice.sql_example_vector_index.load_sql_example_vector_index()
     — the trust/status-filtered (TRUSTED, ACTIVE only), cached immutable
       index.

This module's own job is exactly the sequencing between those: run them in
order, short-circuit on the first failure (a later stage is never invoked
after an earlier one failed), batch and validate embedding-provider calls
the same all-or-nothing way core.sqlservice.embedding_coordinator already
established for schema-document embeddings, time each stage, and assemble
one immutable result a connection-time (or on-demand) caller can act on.

Scope: never calls a real embedding provider itself (injected by the
caller), never generates or executes SQL against a live SQL Server, never
imports golden fixtures or a test module, and is not wired into any route
or the connection-time coordinator — this module is the missing piece that
wiring will later call, not the wiring itself. Never logs provider
exception text, credentials, SQL text, questions, or example prompt/search-
document content — only structural identifiers and counts.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from core.config import (
    SQL_EXAMPLE_EMBEDDING_MAX_BATCH_CHARS,
    SQL_EXAMPLE_EMBEDDING_MAX_BATCH_ITEMS,
)
from core.sqlservice.embedding_providers import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    EmbeddingProviderError,
    ProviderEmbeddingBatchResponse,
)
from core.sqlservice.retrieval_store import (
    StoredRetrievalBuild,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.schema_context_retrieval import SELECTION_REASON_SEMANTIC, ContextDocument
from core.sqlservice.sql_example_embedding_store import (
    STATUS_OK as _EXAMPLE_EMBEDDING_STATUS_OK,
)
from core.sqlservice.sql_example_embedding_store import (
    find_missing_example_search_document_hashes,
    insert_example_embeddings,
)
from core.sqlservice.sql_example_generator import generate_trusted_examples
from core.sqlservice.sql_example_model import STATUS_ACTIVE, TRUST_LEVEL_TRUSTED
from core.sqlservice.sql_example_store import (
    OUTCOME_CREATED,
    OUTCOME_UNOWNED,
    insert_trusted_example,
    list_examples_for_source,
)
from core.sqlservice.sql_example_vector_index import (
    STATUS_OK as _INDEX_STATUS_OK,
)
from core.sqlservice.sql_example_vector_index import (
    STATUS_UNOWNED as _INDEX_STATUS_UNOWNED,
)
from core.sqlservice.sql_example_vector_index import load_sql_example_vector_index

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_OK",
    "STATUS_NOT_PREPARED",
    "STATUS_INSERTION_FAILED",
    "STATUS_EMBEDDING_FAILED",
    "STATUS_VECTOR_INDEX_FAILED",
    "SqlExamplePreparationError",
    "SqlExampleEmbeddingConfigurationError",
    "SqlExampleEmbeddingValidationError",
    "SqlExamplePreparationResult",
    "prepare_source_for_sql_examples",
]

# ── Status vocabulary ──────────────────────────────────────────────────────
#
# One combined namespace spanning every stage this coordinator runs, mirror-
# ing core.sqlservice.retrieval_preparation's own convention exactly: this
# module never invents a finer-grained outer status per underlying
# sub-status, it only says WHICH STAGE failed; the underlying status/message
# is preserved verbatim in `error`.

STATUS_OK = "OK"
STATUS_NOT_PREPARED = "NOT_PREPARED"            # no READY retrieval build for this source — zero generation/provider calls
STATUS_INSERTION_FAILED = "INSERTION_FAILED"    # an insert_trusted_example() call reported UNOWNED after ownership was already confirmed
STATUS_EMBEDDING_FAILED = "EMBEDDING_FAILED"
STATUS_VECTOR_INDEX_FAILED = "VECTOR_INDEX_FAILED"


class SqlExamplePreparationError(Exception):
    """Base for exceptions this module raises itself. EmbeddingConfigurationError/
    EmbeddingProviderError from core.sqlservice.embedding_providers are NOT
    subclasses of this — they propagate unchanged, exactly as in
    core.sqlservice.embedding_coordinator."""


class SqlExampleEmbeddingConfigurationError(SqlExamplePreparationError):
    """Invalid max_batch_items/max_batch_chars configuration — a caller/
    deployment misconfiguration, never a provider or data problem."""


class SqlExampleEmbeddingValidationError(SqlExamplePreparationError):
    """A search-document exceeded the configured batch character limit, or a
    provider's embedding response failed validation (wrong count, bad
    indices, wrong dimensions, non-finite values). Never carries the
    provider's own exception text, SQL, a question, or example prompt
    content — only structural counts/hashes."""


# ── Public result type ────────────────────────────────────────────────────


@dataclass(frozen=True)
class SqlExamplePreparationResult:
    """
    Result of one prepare_source_for_sql_examples() call.

    status: STATUS_OK | STATUS_NOT_PREPARED | STATUS_INSERTION_FAILED |
        STATUS_EMBEDDING_FAILED | STATUS_VECTOR_INDEX_FAILED. Only
        STATUS_OK means the trusted example vector index is actually ready
        to search (which may still mean zero examples exist — see
        indexed_count); every other status means preparation stopped at
        that stage and every field describing a LATER stage is left at its
        not-yet-reached default (0 counts).
    generated_count / skipped_count: from generate_trusted_examples()'s own
        SqlExampleGenerationResult — how many candidates this run's schema
        documents could produce, and how many were skipped by that module's
        own dedup/cap/validation gates.
    examples_inserted / examples_reused: OUTCOME_CREATED / OUTCOME_REUSED
        counts from insert_trusted_example(), one call per generated
        candidate — 0/0 whenever generated_count is 0.
    embeddings_generated / embeddings_reused: mirror
        core.sqlservice.embedding_coordinator.EmbeddingGenerationResult's
        own field semantics, scoped to this source's trusted examples'
        distinct search_document_hash values.
    indexed_count: core.sqlservice.sql_example_vector_index.
        SqlExampleVectorIndexLoadResult.example_count — 0 whenever the
        index stage never ran, never completed, or genuinely has zero
        trusted/active/embedded examples (STATUS_EMPTY is not a failure).
    *_duration_seconds: measured with the (injectable) `clock` — 0.0 for a
        stage that never ran. total_duration_seconds always reflects
        everything actually executed, success or failure.
    """

    status: str
    source_id: int
    build_id: int | None
    schema_fingerprint: str | None
    embedding_provider: str
    embedding_model: str
    embedding_dimensions: int
    generated_count: int
    skipped_count: int
    examples_inserted: int
    examples_reused: int
    embeddings_generated: int
    embeddings_reused: int
    indexed_count: int
    warnings: tuple[str, ...]
    error: str | None
    generation_duration_seconds: float
    insertion_duration_seconds: float
    embedding_duration_seconds: float
    index_load_duration_seconds: float
    total_duration_seconds: float


def _empty_result(
    status: str, source_id: int, provider: EmbeddingProvider, *,
    build_id: int | None = None, schema_fingerprint: str | None = None,
    warnings: tuple[str, ...] = (), error: str | None = None,
    generation_duration: float = 0.0, insertion_duration: float = 0.0,
    embedding_duration: float = 0.0, index_load_duration: float = 0.0,
    generated_count: int = 0, skipped_count: int = 0,
    examples_inserted: int = 0, examples_reused: int = 0,
    embeddings_generated: int = 0, embeddings_reused: int = 0,
    total_duration: float = 0.0,
) -> SqlExamplePreparationResult:
    return SqlExamplePreparationResult(
        status=status,
        source_id=source_id,
        build_id=build_id,
        schema_fingerprint=schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        generated_count=generated_count,
        skipped_count=skipped_count,
        examples_inserted=examples_inserted,
        examples_reused=examples_reused,
        embeddings_generated=embeddings_generated,
        embeddings_reused=embeddings_reused,
        indexed_count=0,
        warnings=warnings,
        error=error,
        generation_duration_seconds=generation_duration,
        insertion_duration_seconds=insertion_duration,
        embedding_duration_seconds=embedding_duration,
        index_load_duration_seconds=index_load_duration,
        total_duration_seconds=total_duration,
    )


# ── Schema-document adaptation (StoredRetrievalDocument -> ContextDocument) ─
#
# generate_trusted_examples() accepts any ContextDocument-shaped sequence
# (see that module's own docstring) and only ever reads object_fqn,
# object_type, retrieval_tier, document_text, and metadata — never `score`
# or `selection_reason` (those describe WHY a question-time retrieval
# pipeline picked a document; this generation run isn't answering a
# question at all). `selection_reason` is still a required field on
# ContextDocument, so a fixed, clearly-named sentinel is used here — never
# one of schema_context_retrieval's own SELECTION_REASON_* values that
# claim provenance the generator never has.

_SYNTHETIC_SELECTION_REASON = SELECTION_REASON_SEMANTIC  # placeholder only — generate_trusted_examples never reads this field


def _to_context_document(doc) -> ContextDocument:
    return ContextDocument(
        object_fqn=doc.object_fqn,
        object_type=doc.object_type,
        retrieval_tier=doc.retrieval_tier,
        document_hash=doc.document_hash,
        document_text=doc.document_text,
        metadata=doc.metadata,
        score=None,
        selection_reason=_SYNTHETIC_SELECTION_REASON,
    )


# ── Embedding batch planning + response validation (pure, no I/O) ─────────
#
# Deliberately mirrors core.sqlservice.embedding_coordinator's own
# _plan_batches/_validate_and_reorder_batch shape exactly rather than
# importing those (private, unexported) functions across modules — the
# same "small pure validators live per-module, never shared cross-module"
# convention core.sqlservice.schema_context_retrieval and
# core.sqlservice.sql_generation_service already document for their own
# duplicated _validate_question. The STORES/PROVIDER/INDEX themselves
# (sql_example_store, sql_example_embedding_store, embedding_providers,
# sql_example_vector_index) are reused unchanged, never duplicated.


def _plan_example_embedding_batches(
    text_by_hash: Mapping[str, str], max_batch_items: int, max_batch_chars: int,
) -> list[list[tuple[str, str]]]:
    batches: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_chars = 0

    for search_document_hash, text in text_by_hash.items():
        text_len = len(text)
        if text_len > max_batch_chars:
            raise SqlExampleEmbeddingValidationError(
                f"a search_document_text exceeds max_batch_chars={max_batch_chars} "
                f"({text_len} characters) and can never fit in any batch"
            )

        would_overflow_items = len(current) + 1 > max_batch_items
        would_overflow_chars = current_chars + text_len > max_batch_chars
        if current and (would_overflow_items or would_overflow_chars):
            batches.append(current)
            current = []
            current_chars = 0

        current.append((search_document_hash, text))
        current_chars += text_len

    if current:
        batches.append(current)
    return batches


def _validate_finite_vector(vector: Sequence[float]) -> None:
    for i, value in enumerate(vector):
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            raise SqlExampleEmbeddingValidationError(f"vector element {i} is not a number") from exc
        if math.isnan(f) or math.isinf(f):
            raise SqlExampleEmbeddingValidationError(f"vector element {i} is not finite")


def _validate_and_reorder_example_batch(
    response: ProviderEmbeddingBatchResponse, batch: list[tuple[str, str]], expected_dimensions: int,
) -> dict[str, tuple[float, ...]]:
    expected_count = len(batch)
    items = response.items

    if len(items) != expected_count:
        raise SqlExampleEmbeddingValidationError(
            f"provider returned {len(items)} embeddings for a batch of {expected_count} search documents"
        )

    by_index: dict[int, tuple[float, ...]] = {}
    for item in items:
        index = item.index
        if not isinstance(index, int) or isinstance(index, bool):
            raise SqlExampleEmbeddingValidationError("provider returned a non-integer index")
        if index < 0 or index >= expected_count:
            raise SqlExampleEmbeddingValidationError(f"provider returned out-of-range index {index}")
        if index in by_index:
            raise SqlExampleEmbeddingValidationError(f"provider returned duplicate index {index}")
        by_index[index] = item.vector

    missing = sorted(set(range(expected_count)) - by_index.keys())
    if missing:
        raise SqlExampleEmbeddingValidationError(f"provider response is missing indices: {missing}")

    result: dict[str, tuple[float, ...]] = {}
    for position, (search_document_hash, _text) in enumerate(batch):
        vector = by_index[position]
        try:
            length = len(vector)
        except TypeError as exc:
            raise SqlExampleEmbeddingValidationError("provider returned a non-sequence vector") from exc
        if length != expected_dimensions:
            raise SqlExampleEmbeddingValidationError(
                f"embedding has {length} dimensions, expected {expected_dimensions}"
            )
        _validate_finite_vector(vector)
        result[search_document_hash] = tuple(float(v) for v in vector)
    return result


# ── Public entry point ────────────────────────────────────────────────────


def prepare_source_for_sql_examples(
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    max_batch_items: int = SQL_EXAMPLE_EMBEDDING_MAX_BATCH_ITEMS,
    max_batch_chars: int = SQL_EXAMPLE_EMBEDDING_MAX_BATCH_CHARS,
    clock: Callable[[], float] = time.perf_counter,
) -> SqlExamplePreparationResult:
    """
    Prepare one source's trusted SQL-example set end to end: deterministic
    generation from the source's latest READY schema build -> idempotent
    trusted-example insertion -> missing-embedding generation -> trusted-
    only vector-index load.

    Runs the four stages in that exact order and short-circuits on the
    first one that doesn't succeed — a later stage is never invoked after
    an earlier one failed.

    Does NOT catch or wrap a provider exception: EmbeddingConfigurationError
    and EmbeddingProviderError from provider.embed_batch() propagate
    completely unchanged, exactly as core.sqlservice.embedding_coordinator
    and core.sqlservice.retrieval_preparation already establish for the
    schema-document embedding stage — a genuinely exceptional condition,
    never a routine one. Because every batch's vectors are accumulated in
    memory and only persisted via ONE insert_example_embeddings() call
    after every batch has succeeded, a provider failure partway through a
    multi-batch run writes ZERO embeddings, not a partial set.
    SqlExampleEmbeddingValidationError (a malformed provider response, or a
    search document too large to ever fit any batch) is likewise never
    caught here — it propagates, for the same reason.

    Only each stage's own EXPECTED, routine typed status (no READY
    build, an ownership mismatch surfacing after this call's own initial
    ownership check, ...) is converted into this function's own
    STATUS_NOT_PREPARED / STATUS_INSERTION_FAILED / STATUS_EMBEDDING_FAILED /
    STATUS_VECTOR_INDEX_FAILED, never raised.

    Idempotent and reuse-safe: a repeat call against an unchanged schema
    build reuses every already-inserted example (by content_hash) and every
    already-cached embedding (by search_document_hash) — zero new provider
    calls when nothing is missing.

    `max_batch_items`/`max_batch_chars` bound the provider batches used to
    embed search_document_text values (SQL_EXAMPLE_EMBEDDING_MAX_BATCH_ITEMS/
    CHARS by default) — core.sqlservice.sql_example_embedding_store.
    insert_example_embeddings() itself enforces no such limit (a single
    atomic write of whatever it's given), so this coordinator is the one
    place those limits are enforced, exactly like
    core.sqlservice.embedding_coordinator does for schema documents.

    Never logs provider exception text, credentials, SQL text, questions,
    or example prompt/search-document content — only structural
    identifiers (source_id, build_id, counts, hash counts).
    """
    total_start = clock()

    def _elapsed(since: float) -> float:
        return clock() - since

    build: StoredRetrievalBuild | None = get_latest_ready_retrieval_build(source_id, user_id)
    if build is None:
        return _empty_result(
            STATUS_NOT_PREPARED, source_id, provider,
            error=f"No READY retrieval build exists for source_id={source_id}.",
            total_duration=_elapsed(total_start),
        )

    # ── Stage 1: deterministic, zero-I/O generation ─────────────────────
    generation_start = clock()
    stored_documents = load_stored_retrieval_documents(build.id, source_id, user_id)
    context_documents = tuple(_to_context_document(d) for d in stored_documents)
    generation_result = generate_trusted_examples(
        context_documents, source_id=source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
    )
    generation_duration = _elapsed(generation_start)

    # ── Stage 2: idempotent trusted-example insertion ───────────────────
    insertion_start = clock()
    examples_inserted = 0
    examples_reused = 0
    for generated in generation_result.candidates:
        insert_result = insert_trusted_example(user_id, generated.candidate)
        if insert_result.outcome == OUTCOME_UNOWNED:
            # Ownership was already confirmed via get_latest_ready_retrieval_build()
            # above — reaching UNOWNED here means source ownership changed
            # mid-call (a genuine race), not a routine condition. Fail
            # closed rather than silently continuing with a partial batch.
            insertion_duration = _elapsed(insertion_start)
            logger.warning(
                "SQL example insertion reported UNOWNED mid-run for source_id=%s build_id=%s",
                source_id, build.id,
            )
            return _empty_result(
                STATUS_INSERTION_FAILED, source_id, provider,
                build_id=build.id, schema_fingerprint=build.schema_fingerprint,
                warnings=generation_result.warnings,
                error=f"source_id={source_id} ownership could not be confirmed during example insertion",
                generation_duration=generation_duration, insertion_duration=insertion_duration,
                generated_count=generation_result.generated_count, skipped_count=generation_result.skipped_count,
                examples_inserted=examples_inserted, examples_reused=examples_reused,
                total_duration=_elapsed(total_start),
            )
        elif insert_result.outcome == OUTCOME_CREATED:
            examples_inserted += 1
        else:
            examples_reused += 1
    insertion_duration = _elapsed(insertion_start)

    # ── Stage 3: generate only missing embeddings, all-or-nothing ───────
    if not isinstance(max_batch_items, int) or isinstance(max_batch_items, bool) or max_batch_items <= 0:
        raise SqlExampleEmbeddingConfigurationError(f"max_batch_items must be a positive integer, got {max_batch_items!r}")
    if not isinstance(max_batch_chars, int) or isinstance(max_batch_chars, bool) or max_batch_chars <= 0:
        raise SqlExampleEmbeddingConfigurationError(f"max_batch_chars must be a positive integer, got {max_batch_chars!r}")

    embedding_start = clock()
    trusted_active = list_examples_for_source(
        source_id, user_id, status=STATUS_ACTIVE, trust_level=TRUST_LEVEL_TRUSTED,
    )
    total_documents = len({ex.search_document_hash for ex in trusted_active})

    missing_hashes = find_missing_example_search_document_hashes(
        source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    if missing_hashes is None:
        embedding_duration = _elapsed(embedding_start)
        logger.warning(
            "SQL example embedding lookup reported UNOWNED mid-run for source_id=%s build_id=%s",
            source_id, build.id,
        )
        return _empty_result(
            STATUS_EMBEDDING_FAILED, source_id, provider,
            build_id=build.id, schema_fingerprint=build.schema_fingerprint,
            warnings=generation_result.warnings,
            error=f"source_id={source_id} ownership could not be confirmed during embedding lookup",
            generation_duration=generation_duration, insertion_duration=insertion_duration,
            embedding_duration=embedding_duration,
            generated_count=generation_result.generated_count, skipped_count=generation_result.skipped_count,
            examples_inserted=examples_inserted, examples_reused=examples_reused,
            total_duration=_elapsed(total_start),
        )

    if not missing_hashes:
        embeddings_generated = 0
        embeddings_reused = total_documents
    else:
        text_by_hash: dict[str, str] = {}
        missing_set = set(missing_hashes)
        for example in trusted_active:
            if example.search_document_hash in missing_set and example.search_document_hash not in text_by_hash:
                text_by_hash[example.search_document_hash] = example.search_document_text

        batches = _plan_example_embedding_batches(text_by_hash, max_batch_items, max_batch_chars)

        accumulated_vectors: dict[str, tuple[float, ...]] = {}
        for batch in batches:
            texts = [text for _hash, text in batch]
            try:
                response = provider.embed_batch(texts)
            except (EmbeddingConfigurationError, EmbeddingProviderError):
                raise  # propagate unchanged — never wrapped, never partially persisted
            except Exception as exc:  # noqa: BLE001 - a misbehaving provider is still a provider failure
                raise EmbeddingProviderError(f"{type(exc).__name__}") from exc

            accumulated_vectors.update(
                _validate_and_reorder_example_batch(response, batch, provider.dimensions)
            )

        insert_embedding_result = insert_example_embeddings(
            source_id, user_id, provider.provider_id, provider.model, provider.dimensions, accumulated_vectors,
        )
        if insert_embedding_result.status != _EXAMPLE_EMBEDDING_STATUS_OK:
            embedding_duration = _elapsed(embedding_start)
            return _empty_result(
                STATUS_EMBEDDING_FAILED, source_id, provider,
                build_id=build.id, schema_fingerprint=build.schema_fingerprint,
                warnings=generation_result.warnings,
                error=f"embedding persistence status={insert_embedding_result.status!r}",
                generation_duration=generation_duration, insertion_duration=insertion_duration,
                embedding_duration=embedding_duration,
                generated_count=generation_result.generated_count, skipped_count=generation_result.skipped_count,
                examples_inserted=examples_inserted, examples_reused=examples_reused,
                total_duration=_elapsed(total_start),
            )

        requested_count = len(text_by_hash)
        embeddings_generated = len(insert_embedding_result.inserted_hashes)
        embeddings_reused = (total_documents - requested_count) + len(insert_embedding_result.reused_hashes)

    embedding_duration = _elapsed(embedding_start)

    # ── Stage 4: trusted-only vector index load ─────────────────────────
    index_load_start = clock()
    index_result = load_sql_example_vector_index(
        source_id, user_id, provider.provider_id, provider.model, provider.dimensions,
    )
    index_load_duration = _elapsed(index_load_start)

    if index_result.status == _INDEX_STATUS_UNOWNED:
        logger.warning(
            "SQL example vector index load reported UNOWNED mid-run for source_id=%s build_id=%s",
            source_id, build.id,
        )
        return _empty_result(
            STATUS_VECTOR_INDEX_FAILED, source_id, provider,
            build_id=build.id, schema_fingerprint=build.schema_fingerprint,
            warnings=generation_result.warnings,
            error=f"source_id={source_id} ownership could not be confirmed during index load",
            generation_duration=generation_duration, insertion_duration=insertion_duration,
            embedding_duration=embedding_duration, index_load_duration=index_load_duration,
            generated_count=generation_result.generated_count, skipped_count=generation_result.skipped_count,
            examples_inserted=examples_inserted, examples_reused=examples_reused,
            embeddings_generated=embeddings_generated, embeddings_reused=embeddings_reused,
            total_duration=_elapsed(total_start),
        )

    # STATUS_OK and STATUS_EMPTY are both non-failure outcomes here — an
    # owned source with genuinely zero trusted/active/embedded examples
    # (STATUS_EMPTY) is a routine, valid end state, never a preparation
    # failure (mirrors STATUS_EMPTY's own documented contract).
    indexed_count = index_result.example_count if index_result.status == _INDEX_STATUS_OK else 0

    total_duration = _elapsed(total_start)
    logger.info(
        "SQL example preparation completed for source_id=%s build_id=%s: "
        "generated=%s inserted=%s reused=%s embeddings_generated=%s embeddings_reused=%s indexed=%s",
        source_id, build.id, generation_result.generated_count, examples_inserted, examples_reused,
        embeddings_generated, embeddings_reused, indexed_count,
    )

    return SqlExamplePreparationResult(
        status=STATUS_OK,
        source_id=source_id,
        build_id=build.id,
        schema_fingerprint=build.schema_fingerprint,
        embedding_provider=provider.provider_id,
        embedding_model=provider.model,
        embedding_dimensions=provider.dimensions,
        generated_count=generation_result.generated_count,
        skipped_count=generation_result.skipped_count,
        examples_inserted=examples_inserted,
        examples_reused=examples_reused,
        embeddings_generated=embeddings_generated,
        embeddings_reused=embeddings_reused,
        indexed_count=indexed_count,
        warnings=generation_result.warnings,
        error=None,
        generation_duration_seconds=generation_duration,
        insertion_duration_seconds=insertion_duration,
        embedding_duration_seconds=embedding_duration,
        index_load_duration_seconds=index_load_duration,
        total_duration_seconds=total_duration,
    )
