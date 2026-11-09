"""
Bounded automatic OBJECT_CORE semantic-enrichment preparation coordinator --
SQL Architecture Correction, Phase 6, Step 4C2 / Step 4C2A.

Mirrors core.sqlservice.semantic_enrichment_preparation's own orchestration
shape (Step 4B, Part 3) for the compact OBJECT_CORE phase specifically:
verify ownership/READY build -> compute per-object OBJECT_CORE cache keys ->
generate (bounded-concurrency) only what's missing -> persist every
validated result -> report a typed OBJECT_CORE readiness status.

Deliberately does NOT merge into a SemanticCatalog or warm the linker/join-
planner index caches itself (unlike the existing COLUMN_DEFERRED
coordinator, core.sqlservice.semantic_enrichment_preparation) -- Step 4C2's
own scope is OBJECT_CORE generation/persistence/readiness only.
core.sqlservice.semantic_catalog.build_semantic_catalog already accepts
OBJECT_CORE-shaped StoredSemanticEnrichment rows (columns=(),
supported_operations=()) with ZERO code changes -- see
tests/test_sqlservice_semantic_enrichment_object_core_linker_compatibility.py
for the end-to-end proof -- so a future step can wire that merge in without
touching this module.

Never calls a real provider itself (the caller supplies a
provider_factory), never runs schema discovery or connects to a live SQL
Server, never generates or executes SQL, and is not wired into any route.
Never logs provider exception text, credentials, or a schema payload --
only structural identifiers, counts, and bounded category labels.

Failure policy (Phase 6, Step 4C2A, Problem 1 -- REPLACES Step 4C2's
original "abort the whole run on any provider/config/factory error"
policy): generate_object_core_semantic_enrichment() now NEVER raises for a
per-batch failure -- every batch's outcome (success, or one of a small,
sanitized BATCH_FAILURE_* category) is collected independently and
returned. This module persists every SUCCESSFUL batch's objects
regardless of what happened to any OTHER batch: with dozens of paid
batches, ONE late provider failure must never discard every earlier,
already-paid-for success. Only a genuinely PRE-DISPATCH failure (an
invalid bound, or the one-time provider-factory identity probe failing
before any batch is ever sent) still propagates and aborts the entire call
with zero provider calls and zero writes.

Transaction policy (Problem 1, requirement 8): ONE OBJECT = ONE ATOMIC
TRANSACTION. core.sqlservice.semantic_enrichment_store.
insert_semantic_enrichment() already commits (or rolls back) each object's
row independently -- this coordinator does not introduce a coarser,
multi-object transaction. An unexpected exception from ONE object's
persist call is caught HERE, that object is marked PERSISTENCE_FAILED (and
therefore correctly reported as still-missing by the next readiness
check), and persistence CONTINUES for every other object -- a single row's
write failure can never discard or block another, already-committed
object's durability, and a rerun's completeness check will simply see the
failed object as missing and retry it (idempotent recovery).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_TOTAL_OBJECTS,
    SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS,
)
from core.sqlservice.retrieval_store import (
    StoredRetrievalBuild,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.semantic_enrichment_phases import (
    PHASE_OBJECT_CORE,
    STATUS_NOT_PREPARED,
    STATUS_OBJECT_CORE_PARTIAL,
    STATUS_OBJECT_CORE_READY,
    readiness_status,
)
from core.sqlservice.semantic_enrichment_service import (
    BATCH_FAILURE_PROVIDER_CALL_FAILED,
    BATCH_FAILURE_PROVIDER_FACTORY_FAILED,
    BATCH_FAILURE_UNEXPECTED_WORKER_ERROR,
    BATCH_FAILURE_VALIDATION_FAILED,
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    SemanticEnrichmentCacheKey,
    SemanticEnrichmentConfigurationError,
    SemanticEnrichmentProvider,
    SemanticEnrichmentProviderError,
    SemanticEnrichmentProviderFactoryError,
    build_object_semantic_inputs,
    compute_object_core_cache_key,
    generate_object_core_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_store import (
    COMPLETENESS_STATUS_UNOWNED,
    OUTCOME_CREATED as _STORE_OUTCOME_CREATED,
)
from core.sqlservice.semantic_enrichment_store import (
    OUTCOME_REPAIRED as _STORE_OUTCOME_REPAIRED,
)
from core.sqlservice.semantic_enrichment_store import (
    OUTCOME_UNOWNED as _STORE_OUTCOME_UNOWNED,
)
from core.sqlservice.semantic_enrichment_store import (
    count_enrichment_completeness,
    insert_semantic_enrichment,
)

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_OBJECT_CORE_READY",
    "STATUS_OBJECT_CORE_PARTIAL",
    "STATUS_NOT_PREPARED",
    "STATUS_UNOWNED",
    "PERSISTENCE_FAILURE_CATEGORY",
    "PROVIDER_LEVEL_FAILURE_CATEGORIES",
    "ObjectCoreEnrichmentPreparationResult",
    "prepare_source_object_core_enrichment",
]

# Ownership was already confirmed once (a READY build exists for this
# user), then reported UNOWNED mid-run -- a genuine race (e.g. the source
# was deleted concurrently), never raised.
STATUS_UNOWNED = "UNOWNED"

# A batch that PASSED strict validation but then failed to persist (an
# unexpected store/database error on at least one of its objects) -- a
# THIRD, distinct outcome from generate_object_core_semantic_enrichment()'s
# own BATCH_FAILURE_* categories (which only ever describe the GENERATION
# stage), assigned here at the PERSISTENCE stage. Never claimed as
# "cached"/persisted (Problem 1, requirement 8: "do not claim provider
# results are cached when their transaction failed").
PERSISTENCE_FAILURE_CATEGORY = "PERSISTENCE_FAILED"

# Categories meaning "the provider CALL itself never completed/returned a
# response" -- as opposed to BATCH_FAILURE_VALIDATION_FAILED, where a
# response WAS received but rejected by strict validation. Used to report
# "successful provider batches" vs. "failed provider batches" separately
# from "validated batches" (Problem 1's observability requirements).
PROVIDER_LEVEL_FAILURE_CATEGORIES = frozenset({
    BATCH_FAILURE_PROVIDER_CALL_FAILED, BATCH_FAILURE_PROVIDER_FACTORY_FAILED,
    BATCH_FAILURE_UNEXPECTED_WORKER_ERROR,
})


@dataclass(frozen=True)
class ObjectCoreEnrichmentPreparationResult:
    """
    Result of one prepare_source_object_core_enrichment() call.

    status: STATUS_OBJECT_CORE_READY | STATUS_OBJECT_CORE_PARTIAL |
        STATUS_NOT_PREPARED | STATUS_UNOWNED. READY requires ALL eligible
        objects to have a current, ACTIVE OBJECT_CORE row (Step 4C2
        requirement 7's default readiness policy — see
        core.sqlservice.semantic_enrichment_phases.readiness_status).
        Only READY/PARTIAL mean generation actually ran; every other status
        means preparation stopped before that and every LATER field is left
        at its not-yet-reached default (0 counts).
    eligible_object_count: Tier 1/2 objects in the source's latest READY
        build (core.sqlservice.semantic_enrichment_service.
        ELIGIBLE_ENRICHMENT_TIERS) — the same eligibility rule OBJECT_CORE
        and COLUMN_DEFERRED both use.
    reused_count / generated_count / repaired_count: see
        core.sqlservice.semantic_enrichment_preparation's own identical
        fields — same meaning, OBJECT_CORE phase only.
    cached_object_count: alias of `reused_count` (Problem 1's own
        observability naming — objects that needed ZERO provider work this
        run because a current, ACTIVE row already existed before it began).
    newly_persisted_object_count: `generated_count + repaired_count` —
        objects actually WRITTEN to the store this run (Problem 1's own
        observability naming).
    dispatched_batch_count: batches this run actually sent to a worker
        thread (== generate_object_core_semantic_enrichment's own
        `batch_count` — every batch appears in `batch_outcomes`/
        `failure_categories_by_batch_id` exactly once).
    successful_provider_batch_count / failed_provider_batch_count: whether
        the PROVIDER CALL itself completed and returned a response — a
        batch that called successfully but then failed STRICT VALIDATION
        still counts as a successful provider call here (see
        `validated_batch_count` for the stricter count, and
        PROVIDER_LEVEL_FAILURE_CATEGORIES for exactly which categories are
        NOT a "successful provider call").
    validated_batch_count: batches whose response PASSED strict validation
        (== generate_object_core_semantic_enrichment's own
        `successful_batch_count`) — a strict subset of
        `successful_provider_batch_count`.
    persisted_batch_count: validated batches where EVERY one of their
        objects was ALSO successfully committed to the store (no
        PERSISTENCE_FAILED object among them) — the batches a resumed run
        will never need to touch again.
    missing_object_count: `len(missing_object_fqns)`.
    failure_categories_by_batch_id: EVERY batch that did not end up fully
        persisted this run, keyed by its stable 0-based batch id, mapped to
        exactly one of generate_object_core_semantic_enrichment's
        BATCH_FAILURE_* categories or (Problem 1, requirement 8, persistence
        stage) PERSISTENCE_FAILURE_CATEGORY. A batch present here is never
        counted in `persisted_batch_count`.
    resumable: True exactly when status == STATUS_OBJECT_CORE_PARTIAL (there
        is at least one missing object a follow-up call could still make
        progress on); False for READY (nothing left to do), NOT_PREPARED,
        and UNOWNED (a follow-up call must first resolve the blocking
        condition, not merely retry generation).
    skipped_count / batch_validation_failed_count: from
        generate_object_core_semantic_enrichment()'s own SkippedObject
        accounting; `batch_validation_failed_count` here is recomputed from
        `batch_outcomes` and counts ONLY BATCH_FAILURE_VALIDATION_FAILED
        batches (a strict-validation rejection), distinct from a
        provider-level failure.
    missing_object_fqns: every eligible object WITHOUT a current, ACTIVE
        OBJECT_CORE row after this run — drives the READY/PARTIAL
        determination; empty exactly when status is
        STATUS_OBJECT_CORE_READY.
    concurrency_used: the ACTUAL bounded worker count this run's
        ThreadPoolExecutor dispatched with (0 when generation never ran).
    *_duration_seconds: measured with the (injectable) `clock` — 0.0 for a
        stage that never ran.
    """

    status: str
    source_id: int
    build_id: int | None
    schema_fingerprint: str | None
    provider_id: str
    model: str
    contract_version: int
    eligible_object_count: int
    reused_count: int
    generated_count: int
    repaired_count: int
    cached_object_count: int
    newly_persisted_object_count: int
    skipped_count: int
    batch_validation_failed_count: int
    dispatched_batch_count: int
    successful_provider_batch_count: int
    failed_provider_batch_count: int
    validated_batch_count: int
    persisted_batch_count: int
    batch_count: int  # kept for continuity with Step 4C2 — same as dispatched_batch_count
    concurrency_used: int
    missing_object_count: int
    missing_object_fqns: tuple[str, ...]
    failure_categories_by_batch_id: Mapping[int, str]
    resumable: bool
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    error: str | None
    lookup_duration_seconds: float
    generation_duration_seconds: float
    persistence_duration_seconds: float
    total_duration_seconds: float


def _empty_result(
    status: str, source_id: int, provider_id: str, model: str, *,
    build_id: int | None = None, schema_fingerprint: str | None = None,
    eligible_object_count: int = 0, reused_count: int = 0, generated_count: int = 0, repaired_count: int = 0,
    skipped_count: int = 0, batch_validation_failed_count: int = 0,
    dispatched_batch_count: int = 0, successful_provider_batch_count: int = 0,
    failed_provider_batch_count: int = 0, validated_batch_count: int = 0, persisted_batch_count: int = 0,
    concurrency_used: int = 0, missing_object_fqns: tuple[str, ...] = (),
    failure_categories_by_batch_id: Mapping[int, str] | None = None,
    warnings: tuple[str, ...] = (), error: str | None = None,
    lookup_duration: float = 0.0, generation_duration: float = 0.0, persistence_duration: float = 0.0,
    total_duration: float = 0.0,
) -> ObjectCoreEnrichmentPreparationResult:
    return ObjectCoreEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id=provider_id, model=model, contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        eligible_object_count=eligible_object_count, reused_count=reused_count, generated_count=generated_count,
        repaired_count=repaired_count, cached_object_count=reused_count,
        newly_persisted_object_count=generated_count + repaired_count,
        skipped_count=skipped_count, batch_validation_failed_count=batch_validation_failed_count,
        dispatched_batch_count=dispatched_batch_count, successful_provider_batch_count=successful_provider_batch_count,
        failed_provider_batch_count=failed_provider_batch_count, validated_batch_count=validated_batch_count,
        persisted_batch_count=persisted_batch_count, batch_count=dispatched_batch_count,
        concurrency_used=concurrency_used, missing_object_count=len(missing_object_fqns),
        missing_object_fqns=missing_object_fqns,
        failure_categories_by_batch_id=dict(failure_categories_by_batch_id or {}),
        resumable=(status == STATUS_OBJECT_CORE_PARTIAL),
        warnings=warnings, errors=(), error=error,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, total_duration_seconds=total_duration,
    )


def _probe_provider_identity(provider_factory: Callable[[], SemanticEnrichmentProvider]) -> tuple[str, str]:
    """One factory call, purely to read (provider_id, model) — needed for
    this coordinator's OWN cache lookups before generation ever runs. Never
    itself dispatched to a worker thread. Raises exactly like
    generate_object_core_semantic_enrichment's identical pre-flight probe —
    a broken factory here means ZERO provider calls and zero writes happen
    at all (Phase 6 Step 4C2A Problem 1, requirements 1 and 6)."""
    try:
        provider = provider_factory()
    except (SemanticEnrichmentConfigurationError, SemanticEnrichmentProviderError):
        raise
    except Exception as exc:  # noqa: BLE001 - a misbehaving factory is still a factory failure
        raise SemanticEnrichmentProviderFactoryError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(provider, SemanticEnrichmentProvider):
        raise SemanticEnrichmentProviderFactoryError(
            f"provider_factory() must return a real SemanticEnrichmentProvider, got {type(provider).__name__}"
        )
    return provider.provider_id, provider.model


def prepare_source_object_core_enrichment(
    source_id: int,
    user_id: str,
    provider_factory: Callable[[], SemanticEnrichmentProvider],
    *,
    max_objects_per_batch: int = SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_OBJECTS_PER_BATCH,
    max_batch_characters: int = SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_BATCH_CHARACTERS,
    max_total_objects: int = SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_TOTAL_OBJECTS,
    max_estimated_output_tokens: int = SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_MAX_ESTIMATED_OUTPUT_TOKENS,
    max_output_tokens: int = SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS,
    max_concurrency: int = SQL_SEMANTIC_ENRICHMENT_OBJECT_CORE_DEFAULT_CONCURRENCY,
    clock: Callable[[], float] = time.perf_counter,
) -> ObjectCoreEnrichmentPreparationResult:
    """
    Prepare one source's OBJECT_CORE semantic enrichment end to end: verify
    ownership and the latest READY retrieval build -> load its documents ->
    compute each eligible object's OBJECT_CORE cache key -> load exactly-
    matching cached results -> dispatch the provider (bounded concurrency)
    ONLY for missing/changed objects -> strictly validate -> persist EVERY
    successful batch independently -> determine OBJECT_CORE readiness from
    the store's OWN completeness count (never from generation's in-memory
    bookkeeping alone — a resumed run that persists nothing new because
    everything was already current must still correctly report READY).

    Idempotent and resumable, exactly like core.sqlservice.
    semantic_enrichment_preparation.prepare_source_semantic_enrichment: a
    repeat call against an unchanged schema build makes ZERO provider
    calls; a call after one object's structure/dictionary content or its
    informative-column selection changed re-enriches ONLY that object; a
    call after a PARTIAL run re-enriches ONLY the objects still missing
    (Phase 6 Step 4C2A Problem 1, requirement 4) — never re-purchases an
    already-successfully-persisted object's batch.

    Failure handling (Problem 1 — see this module's own docstring for the
    full policy): a per-batch provider/validation failure during dispatch
    is isolated to that batch by generate_object_core_semantic_enrichment
    itself and never raised here; every OTHER batch's objects are still
    persisted. Only a PRE-DISPATCH provider/factory/configuration failure
    (the identity probe above, or an invalid bound) still propagates
    UNCHANGED, with NOTHING persisted (persistence only ever happens AFTER
    generate_object_core_semantic_enrichment() returns).

    An unexpected PERSISTENCE failure for one object (a store/database
    error, never expected in normal operation) is caught, sanitized, and
    isolated to that object alone (never that object's whole batch, and
    never any other batch) — see PERSISTENCE_FAILURE_CATEGORY.

    Never logs provider exception text, credentials, or a schema payload —
    only structural identifiers, counts, and bounded category labels.
    """
    total_start = clock()

    def _elapsed(since: float) -> float:
        return clock() - since

    provider_id, model = _probe_provider_identity(provider_factory)

    build: StoredRetrievalBuild | None = get_latest_ready_retrieval_build(source_id, user_id)
    if build is None:
        return _empty_result(
            STATUS_NOT_PREPARED, source_id, provider_id, model,
            error=f"No READY retrieval build exists for source_id={source_id}.",
            total_duration=_elapsed(total_start),
        )

    stored_documents = load_stored_retrieval_documents(build.id, source_id, user_id)

    # ── Stage 1: deterministic per-object OBJECT_CORE cache-key lookup ────
    lookup_start = clock()
    obj_inputs, _ineligible_skips = build_object_semantic_inputs(stored_documents)
    eligible_object_count = len(obj_inputs)

    cache_keys_by_fqn: dict[str, SemanticEnrichmentCacheKey] = {
        obj.object_fqn: compute_object_core_cache_key(obj, provider_id, model) for obj in obj_inputs
    }
    current_hashes_by_object_fqn = {fqn: key.semantic_input_hash for fqn, key in cache_keys_by_fqn.items()}

    completeness_before = count_enrichment_completeness(
        source_id, user_id, provider_id=provider_id, model=model,
        contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn, phase=PHASE_OBJECT_CORE,
    )
    if completeness_before.status == COMPLETENESS_STATUS_UNOWNED:
        return _empty_result(
            STATUS_UNOWNED, source_id, provider_id, model, build_id=build.id,
            schema_fingerprint=build.schema_fingerprint,
            error=f"source_id={source_id} ownership could not be confirmed",
            total_duration=_elapsed(total_start),
        )

    reused_before_generation = completeness_before.cached_current_count
    already_cached_keys = frozenset(
        cache_keys_by_fqn[fqn] for fqn in current_hashes_by_object_fqn
        if fqn not in completeness_before.missing_object_fqns
    )
    lookup_duration = _elapsed(lookup_start)

    # ── Stage 2: bounded-concurrency dispatch for missing/changed objects.
    #    NEVER raises for a per-batch failure -- see module docstring. ────
    generation_start = clock()
    generation_result = generate_object_core_semantic_enrichment(
        stored_documents, provider_factory, schema_fingerprint=build.schema_fingerprint,
        already_cached_keys=already_cached_keys,
        max_objects_per_batch=max_objects_per_batch, max_batch_characters=max_batch_characters,
        max_total_objects=max_total_objects, max_estimated_output_tokens=max_estimated_output_tokens,
        max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency, clock=clock,
    )
    generation_duration = _elapsed(generation_start)

    object_fqn_to_batch_id = {
        fqn: outcome.batch_id for outcome in generation_result.batch_outcomes if outcome.succeeded
        for fqn in outcome.object_fqns
    }

    # ── Stage 3: persist EVERY validated object independently. ONE OBJECT
    #    = ONE ATOMIC TRANSACTION (see module docstring) -- an unexpected
    #    persistence failure for one object never blocks or discards any
    #    other object's (or batch's) persistence. ───────────────────────
    persistence_start = clock()
    generated_count = 0
    repaired_count = 0
    reused_after_generation = 0
    persistence_failed_fqns: set[str] = set()
    for obj in generation_result.enriched_objects:
        cache_key = generation_result.cache_keys_by_object_fqn[obj.object_fqn]
        try:
            insert_result = insert_semantic_enrichment(
                user_id, source_id=source_id, object_fqn=obj.object_fqn,
                semantic_input_hash=cache_key.semantic_input_hash,
                provider_id=provider_id, model=model,
                contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION, phase=PHASE_OBJECT_CORE,
                object_semantics=obj, column_semantics=(),
            )
        except Exception as exc:  # noqa: BLE001 - isolate to this ONE object; never abort the loop
            persistence_failed_fqns.add(obj.object_fqn)
            logger.warning(
                "OBJECT_CORE enrichment persistence failed for one object under source_id=%s build_id=%s "
                "(%s) -- object remains missing and will be retried on the next call",
                source_id, build.id, type(exc).__name__,
            )
            continue

        if insert_result.outcome == _STORE_OUTCOME_UNOWNED:
            persistence_duration = _elapsed(persistence_start)
            logger.warning(
                "OBJECT_CORE enrichment persistence reported UNOWNED mid-run for source_id=%s build_id=%s",
                source_id, build.id,
            )
            return _empty_result(
                STATUS_UNOWNED, source_id, provider_id, model, build_id=build.id,
                schema_fingerprint=build.schema_fingerprint, eligible_object_count=eligible_object_count,
                reused_count=reused_before_generation, generated_count=generated_count, repaired_count=repaired_count,
                warnings=generation_result.warnings,
                error=f"source_id={source_id} ownership could not be confirmed during enrichment persistence",
                lookup_duration=lookup_duration, generation_duration=generation_duration,
                persistence_duration=persistence_duration, total_duration=_elapsed(total_start),
            )
        elif insert_result.outcome == _STORE_OUTCOME_CREATED:
            generated_count += 1
        elif insert_result.outcome == _STORE_OUTCOME_REPAIRED:
            repaired_count += 1
        else:
            reused_after_generation += 1
    persistence_duration = _elapsed(persistence_start)

    # ── Stage 4: readiness from the STORE's own ground truth, never from
    #    generation's in-memory bookkeeping alone ─────────────────────────
    completeness_after = count_enrichment_completeness(
        source_id, user_id, provider_id=provider_id, model=model,
        contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn, phase=PHASE_OBJECT_CORE,
    )
    missing_object_fqns = completeness_after.missing_object_fqns
    status = readiness_status(
        PHASE_OBJECT_CORE, eligible_count=eligible_object_count, missing_count=len(missing_object_fqns),
    )

    # ── Observability (Problem 1) ────────────────────────────────────────
    batch_ids_with_persistence_failure = {
        object_fqn_to_batch_id[fqn] for fqn in persistence_failed_fqns if fqn in object_fqn_to_batch_id
    }
    failure_categories_by_batch_id: dict[int, str] = {
        outcome.batch_id: outcome.failure_category
        for outcome in generation_result.batch_outcomes if not outcome.succeeded
    }
    for batch_id in batch_ids_with_persistence_failure:
        failure_categories_by_batch_id[batch_id] = PERSISTENCE_FAILURE_CATEGORY

    batch_validation_failed_count = sum(
        1 for outcome in generation_result.batch_outcomes
        if not outcome.succeeded and outcome.failure_category == BATCH_FAILURE_VALIDATION_FAILED
    )
    failed_provider_batch_count = sum(
        1 for outcome in generation_result.batch_outcomes
        if not outcome.succeeded and outcome.failure_category in PROVIDER_LEVEL_FAILURE_CATEGORIES
    )
    successful_provider_batch_count = generation_result.batch_count - failed_provider_batch_count
    validated_batch_count = generation_result.successful_batch_count
    persisted_batch_count = validated_batch_count - len(batch_ids_with_persistence_failure)

    total_duration = _elapsed(total_start)

    logger.info(
        "OBJECT_CORE enrichment preparation completed for source_id=%s build_id=%s status=%s: "
        "eligible=%s cached=%s newly_persisted=%s missing=%s dispatched_batches=%s validated_batches=%s "
        "persisted_batches=%s failed_batches=%s concurrency=%s",
        source_id, build.id, status, eligible_object_count,
        reused_before_generation + reused_after_generation, generated_count + repaired_count,
        len(missing_object_fqns), generation_result.batch_count, validated_batch_count,
        persisted_batch_count, len(failure_categories_by_batch_id), generation_result.concurrency_used,
    )

    return ObjectCoreEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
        provider_id=provider_id, model=model, contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        eligible_object_count=eligible_object_count,
        reused_count=reused_before_generation + reused_after_generation,
        generated_count=generated_count, repaired_count=repaired_count,
        cached_object_count=reused_before_generation + reused_after_generation,
        newly_persisted_object_count=generated_count + repaired_count,
        skipped_count=generation_result.skipped_count, batch_validation_failed_count=batch_validation_failed_count,
        dispatched_batch_count=generation_result.batch_count,
        successful_provider_batch_count=successful_provider_batch_count,
        failed_provider_batch_count=failed_provider_batch_count,
        validated_batch_count=validated_batch_count, persisted_batch_count=persisted_batch_count,
        batch_count=generation_result.batch_count, concurrency_used=generation_result.concurrency_used,
        missing_object_count=len(missing_object_fqns), missing_object_fqns=missing_object_fqns,
        failure_categories_by_batch_id=dict(failure_categories_by_batch_id),
        resumable=(status == STATUS_OBJECT_CORE_PARTIAL),
        warnings=generation_result.warnings, errors=generation_result.errors, error=None,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, total_duration_seconds=total_duration,
    )
