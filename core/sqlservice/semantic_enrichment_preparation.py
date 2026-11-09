"""
Bounded automatic semantic-enrichment preparation coordinator — SQL
Architecture Correction, Phase 6, Step 4B, Part 3.

One typed orchestration function over existing, already-tested building
blocks — it does not duplicate any of their logic:

  1. core.sqlservice.retrieval_store.get_latest_ready_retrieval_build() /
     load_stored_retrieval_documents()
     — the ownership/READY gate for the source's latest schema build, and
       its persisted documents.
  2. core.sqlservice.semantic_enrichment_service.build_object_semantic_inputs()
     / compute_object_semantic_input_hash() / generate_semantic_enrichment()
     — deterministic per-object input hashing and the bounded, batched,
       strictly-validated provider call sequence (Step 4A). This module
       never re-derives a semantic-input hash or re-implements batching.
  3. core.sqlservice.semantic_enrichment_store.load_current_enrichments_for_source()
     / insert_semantic_enrichment()
     — the exact-cache-key lookup that drives "call the provider only for
       missing/changed objects", and the ONLY way to persist a validated
       result.
  4. core.sqlservice.semantic_catalog.build_semantic_catalog(enrichment_input=...)
     — the automatic advisory merge (Step 4B, Part 4).
  5. core.sqlservice.schema_linker.prepare_linker_index() /
     core.sqlservice.join_planner.prepare_join_graph_index()
     — warms both existing, unchanged index caches for the merged catalog/
       documents (Step 4B, Part 6) — no question is ever involved.

This module's own job is exactly the sequencing between those: run them in
order, short-circuit on the first ownership/READY-build failure, persist
every validated result before ever building a catalog from it, and time
each stage.

Failure policy: SemanticEnrichmentConfigurationError, SemanticEnrichment
ProviderError, and SemanticEnrichmentBatchLimitError from step 2 propagate
COMPLETELY UNCHANGED — never caught, never wrapped, exactly like
core.sqlservice.embedding_coordinator and core.sqlservice.
sql_example_preparation already establish for their own provider stages. A
batch-response VALIDATION failure, however, is NOT an exception here at
all — core.sqlservice.semantic_enrichment_service.generate_semantic_
enrichment() already isolates that to the one offending batch (see that
module's own docstring) and reports it via `skipped_objects`/`warnings` on
its result; this coordinator surfaces that as STATUS_PARTIAL (never raises)
and still persists every OTHER batch's validated results — "partial valid
results may be persisted, but catalog completeness and skipped batches must
be reported honestly" (Part 3 requirement).

Scope: never calls a real provider itself (injected by the caller), never
runs schema discovery or connects to a live SQL Server, never generates or
executes SQL, and is not wired into any route or the connection-time
coordinator. Never logs provider exception text, credentials, or a schema
payload — only structural identifiers and counts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS,
    SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS,
)
from core.sqlservice.join_planner import prepare_join_graph_index
from core.sqlservice.retrieval_store import (
    StoredRetrievalBuild,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.schema_linker import prepare_linker_index
from core.sqlservice.semantic_catalog import SemanticCatalog, build_semantic_catalog
from core.sqlservice.semantic_enrichment_service import (
    ELIGIBLE_ENRICHMENT_TIERS,
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    SKIP_REASON_BATCH_VALIDATION_FAILED,
    SemanticEnrichmentCacheKey,
    SemanticEnrichmentProvider,
    build_object_semantic_inputs,
    compute_object_semantic_input_hash,
    generate_semantic_enrichment,
)
from core.sqlservice.semantic_enrichment_store import (
    OUTCOME_CREATED as _STORE_OUTCOME_CREATED,
)
from core.sqlservice.semantic_enrichment_store import (
    OUTCOME_REPAIRED as _STORE_OUTCOME_REPAIRED,
)
from core.sqlservice.semantic_enrichment_store import (
    OUTCOME_UNOWNED as _STORE_OUTCOME_UNOWNED,
)
from core.sqlservice.semantic_enrichment_store import (
    insert_semantic_enrichment,
    load_current_enrichments_for_source,
)

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_OK",
    "STATUS_PARTIAL",
    "STATUS_NOT_PREPARED",
    "STATUS_UNOWNED",
    "SemanticEnrichmentPreparationResult",
    "prepare_source_semantic_enrichment",
]

# ── Status vocabulary ──────────────────────────────────────────────────────
#
# One combined namespace, mirroring core.sqlservice.retrieval_preparation's
# and core.sqlservice.sql_example_preparation's own convention exactly.

STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"          # at least one provider batch failed structured-response validation
STATUS_NOT_PREPARED = "NOT_PREPARED"  # no READY retrieval build for this source — zero provider/store calls
STATUS_UNOWNED = "UNOWNED"          # ownership was already confirmed, then reported UNOWNED mid-run (a genuine race)


@dataclass(frozen=True)
class SemanticEnrichmentPreparationResult:
    """
    Result of one prepare_source_semantic_enrichment() call.

    status: STATUS_OK | STATUS_PARTIAL | STATUS_NOT_PREPARED | STATUS_UNOWNED.
        Only STATUS_OK/STATUS_PARTIAL mean a merged SemanticCatalog was
        actually built and the linker/join-planner caches were actually
        warmed; every other status means preparation stopped before that
        and every field describing a LATER stage is left at its
        not-yet-reached default (0 counts, None identity).
    eligible_object_count: Tier 1/2 objects in the source's latest READY
        build (core.sqlservice.semantic_enrichment_service.
        ELIGIBLE_ENRICHMENT_TIERS) — the maximum this run could ever enrich.
    objects_missing_typed_descriptors: how many of those eligible objects
        have no "column_descriptors" metadata at all (SQL Architecture
        Correction, Phase 6, Step 1A field; Step 4B Part 5 observability —
        such an object's columns fall back to the legacy column_names/
        primary_key_columns/confirmed_relationship_edges path and are
        reported as normalized_sql_type="OTHER", never guessed).
    reused_count: objects that required ZERO provider work this run —
        already had a current, ACTIVE, decodable enrichment row before
        generation started, PLUS any object the provider was asked about
        but which turned out (by exact cache key) to already be persisted
        by the time this run tried to insert it (a rare concurrent-run
        edge case, store outcome REPAIRED/REUSED are still zero-provider-
        call-relative-to-THIS-run in spirit — REPAIRED is its own field
        below, kept separate for honesty since content WAS rewritten).
    generated_count: NEW rows actually created and persisted this run.
    repaired_count: existing INVALID/corrupt rows healed in place this run
        (see core.sqlservice.semantic_enrichment_store.OUTCOME_REPAIRED) —
        what makes this coordinator safely resumable after an interruption.
    skipped_count / batch_validation_failed_count: from generate_semantic_
        enrichment()'s own SkippedObject accounting — skipped_count is the
        total (ineligible tier + total-object-cap truncation + batch-
        validation failures + partial provider coverage);
        batch_validation_failed_count is the subset that specifically
        drives status=STATUS_PARTIAL.
    catalog_object_count / catalog_column_count: len() of the merged
        SemanticCatalog's own model_inferred_object_evidence/
        model_inferred_column_evidence — how much USABLE enrichment
        actually made it into the catalog (dictionary-confirmed objects
        still get candidates recorded — see that module's own docstring).
    *_duration_seconds: measured with the (injectable) `clock` — 0.0 for a
        stage that never ran. total_duration_seconds always reflects
        everything actually executed, success or failure.
    """

    status: str
    source_id: int
    build_id: int | None
    schema_fingerprint: str | None
    provider_id: str
    model: str
    contract_version: int
    eligible_object_count: int
    objects_missing_typed_descriptors: int
    reused_count: int
    generated_count: int
    repaired_count: int
    skipped_count: int
    batch_validation_failed_count: int
    batch_count: int
    catalog_object_count: int
    catalog_column_count: int
    catalog_fingerprint: str | None
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    error: str | None
    lookup_duration_seconds: float
    generation_duration_seconds: float
    persistence_duration_seconds: float
    catalog_build_duration_seconds: float
    index_preparation_duration_seconds: float
    total_duration_seconds: float


def _empty_result(
    status: str, source_id: int, provider: SemanticEnrichmentProvider, *,
    build_id: int | None = None, schema_fingerprint: str | None = None,
    eligible_object_count: int = 0, objects_missing_typed_descriptors: int = 0,
    reused_count: int = 0, generated_count: int = 0, repaired_count: int = 0,
    skipped_count: int = 0, batch_validation_failed_count: int = 0, batch_count: int = 0,
    warnings: tuple[str, ...] = (), error: str | None = None,
    lookup_duration: float = 0.0, generation_duration: float = 0.0, persistence_duration: float = 0.0,
    catalog_build_duration: float = 0.0, index_preparation_duration: float = 0.0, total_duration: float = 0.0,
) -> SemanticEnrichmentPreparationResult:
    return SemanticEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id=provider.provider_id, model=provider.model, contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        eligible_object_count=eligible_object_count, objects_missing_typed_descriptors=objects_missing_typed_descriptors,
        reused_count=reused_count, generated_count=generated_count, repaired_count=repaired_count,
        skipped_count=skipped_count, batch_validation_failed_count=batch_validation_failed_count,
        batch_count=batch_count, catalog_object_count=0, catalog_column_count=0, catalog_fingerprint=None,
        warnings=warnings, errors=(), error=error,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, catalog_build_duration_seconds=catalog_build_duration,
        index_preparation_duration_seconds=index_preparation_duration, total_duration_seconds=total_duration,
    )


def _typed_descriptor_gap_count(stored_documents) -> int:
    return sum(
        1 for doc in stored_documents
        if doc.retrieval_tier in ELIGIBLE_ENRICHMENT_TIERS and not doc.metadata.get("column_descriptors")
    )


def _cache_key_from_stored(entry: object) -> SemanticEnrichmentCacheKey:
    return SemanticEnrichmentCacheKey(
        object_fqn=entry.object_fqn, semantic_input_hash=entry.semantic_input_hash,
        provider_id=entry.provider_id, model=entry.model, contract_version=entry.contract_version,
    )


def prepare_source_semantic_enrichment(
    source_id: int,
    user_id: str,
    provider: SemanticEnrichmentProvider,
    *,
    max_objects_per_batch: int = SQL_SEMANTIC_ENRICHMENT_MAX_OBJECTS_PER_BATCH,
    max_columns_per_batch: int = SQL_SEMANTIC_ENRICHMENT_MAX_COLUMNS_PER_BATCH,
    max_batch_characters: int = SQL_SEMANTIC_ENRICHMENT_MAX_BATCH_CHARACTERS,
    max_total_objects: int = SQL_SEMANTIC_ENRICHMENT_MAX_TOTAL_OBJECTS,
    max_output_tokens: int = SQL_SEMANTIC_ENRICHMENT_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = SQL_SEMANTIC_ENRICHMENT_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.perf_counter,
) -> SemanticEnrichmentPreparationResult:
    """
    Prepare one source's semantic enrichment end to end: verify ownership
    and the latest READY retrieval build -> load its documents -> build
    deterministic per-object semantic-input hashes -> load exactly-matching
    cached results -> call the provider ONLY for missing/changed objects ->
    strictly validate -> persist -> reload the complete current set ->
    merge into a SemanticCatalog -> warm the linker/join-planner index
    caches for that catalog. No question is ever involved.

    Idempotent and reuse-safe: a repeat call against an unchanged schema
    build makes ZERO provider calls (every object is already current in
    the store); a call after ONE object's structure/dictionary content
    changed re-enriches ONLY that object (see core.sqlservice.
    semantic_enrichment_service's own per-object content-addressed cache
    key contract) — every other object's enrichment is reused unchanged.

    Does NOT catch or wrap a provider exception: SemanticEnrichment
    ConfigurationError, SemanticEnrichmentProviderError, and
    SemanticEnrichmentBatchLimitError from generate_semantic_enrichment()
    propagate completely unchanged, exactly as core.sqlservice.
    embedding_coordinator and core.sqlservice.sql_example_preparation
    already establish for their own provider stages — a genuinely
    exceptional condition, never a routine one, with NOTHING new persisted
    (persistence only ever happens AFTER generate_semantic_enrichment()
    returns successfully).

    A batch-response VALIDATION failure is different: generate_semantic_
    enrichment() already isolates it to the one offending batch (see that
    module's own docstring) rather than raising, so this function instead
    reports status=STATUS_PARTIAL — every OTHER batch's validated objects
    are still persisted and merged into the catalog; only the skipped
    batch's objects are absent, and exactly how many is reported via
    `batch_validation_failed_count`/`skipped_count` (never silently
    dropped, never conflated with a routine skip like an ineligible tier).

    Only each stage's own EXPECTED, routine typed status (no READY build,
    an ownership mismatch surfacing mid-run) is converted into this
    function's own STATUS_NOT_PREPARED/STATUS_UNOWNED, never raised.

    Never logs provider exception text, credentials, or a schema payload —
    only structural identifiers and counts.
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

    stored_documents = load_stored_retrieval_documents(build.id, source_id, user_id)

    # ── Stage 1: deterministic per-object input hashing + exact-cache lookup
    lookup_start = clock()
    obj_inputs, _ineligible_skips = build_object_semantic_inputs(stored_documents)
    eligible_object_count = len(obj_inputs)
    objects_missing_typed_descriptors = _typed_descriptor_gap_count(stored_documents)

    current_hashes_by_object_fqn = {
        obj.object_fqn: compute_object_semantic_input_hash(obj) for obj in obj_inputs
    }
    current_enrichments = load_current_enrichments_for_source(
        source_id, user_id, provider_id=provider.provider_id, model=provider.model,
        contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn,
    )
    reused_before_generation = len(current_enrichments)
    already_cached_keys = frozenset(_cache_key_from_stored(e) for e in current_enrichments)
    lookup_duration = _elapsed(lookup_start)

    # ── Stage 2: call the provider only for missing/changed objects ────────
    generation_start = clock()
    generation_result = generate_semantic_enrichment(
        stored_documents, provider, schema_fingerprint=build.schema_fingerprint,
        already_cached_keys=already_cached_keys,
        max_objects_per_batch=max_objects_per_batch, max_columns_per_batch=max_columns_per_batch,
        max_batch_characters=max_batch_characters, max_total_objects=max_total_objects,
        max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds, clock=clock,
    )
    generation_duration = _elapsed(generation_start)

    # ── Stage 3: persist every validated result ─────────────────────────
    persistence_start = clock()
    columns_by_object_fqn: dict[str, list] = {}
    for col in generation_result.enriched_columns:
        columns_by_object_fqn.setdefault(col.object_fqn, []).append(col)

    generated_count = 0
    repaired_count = 0
    reused_after_generation = 0
    for obj in generation_result.enriched_objects:
        cache_key = generation_result.cache_keys_by_object_fqn[obj.object_fqn]
        insert_result = insert_semantic_enrichment(
            user_id, source_id=source_id, object_fqn=obj.object_fqn,
            semantic_input_hash=cache_key.semantic_input_hash,
            provider_id=provider.provider_id, model=provider.model,
            contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
            object_semantics=obj, column_semantics=tuple(columns_by_object_fqn.get(obj.object_fqn, ())),
        )
        if insert_result.outcome == _STORE_OUTCOME_UNOWNED:
            persistence_duration = _elapsed(persistence_start)
            logger.warning(
                "Semantic enrichment persistence reported UNOWNED mid-run for source_id=%s build_id=%s",
                source_id, build.id,
            )
            return _empty_result(
                STATUS_UNOWNED, source_id, provider, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
                eligible_object_count=eligible_object_count,
                objects_missing_typed_descriptors=objects_missing_typed_descriptors,
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

    # ── Stage 4: reload the COMPLETE current set for the catalog merge ──
    complete_current = load_current_enrichments_for_source(
        source_id, user_id, provider_id=provider.provider_id, model=provider.model,
        contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn,
    )

    # ── Stage 5: automatic advisory merge into a SemanticCatalog ────────
    catalog_start = clock()
    catalog: SemanticCatalog = build_semantic_catalog(
        stored_documents, schema_fingerprint=build.schema_fingerprint, build_id=build.id,
        enrichment_input=complete_current,
    )
    catalog_duration = _elapsed(catalog_start)

    # ── Stage 6: warm the existing linker/join-planner index caches ────
    index_start = clock()
    prepare_linker_index(catalog, stored_documents)
    prepare_join_graph_index(stored_documents)
    index_duration = _elapsed(index_start)

    batch_validation_failed_count = sum(
        1 for s in generation_result.skipped_objects if s.reason == SKIP_REASON_BATCH_VALIDATION_FAILED
    )
    status = STATUS_PARTIAL if batch_validation_failed_count > 0 else STATUS_OK
    total_duration = _elapsed(total_start)

    logger.info(
        "Semantic enrichment preparation completed for source_id=%s build_id=%s status=%s: "
        "eligible=%s reused=%s generated=%s repaired=%s skipped=%s batches=%s",
        source_id, build.id, status, eligible_object_count,
        reused_before_generation + reused_after_generation, generated_count, repaired_count,
        generation_result.skipped_count, generation_result.batch_count,
    )

    return SemanticEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
        provider_id=provider.provider_id, model=provider.model, contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
        eligible_object_count=eligible_object_count,
        objects_missing_typed_descriptors=objects_missing_typed_descriptors,
        reused_count=reused_before_generation + reused_after_generation,
        generated_count=generated_count, repaired_count=repaired_count,
        skipped_count=generation_result.skipped_count, batch_validation_failed_count=batch_validation_failed_count,
        batch_count=generation_result.batch_count,
        catalog_object_count=len(catalog.model_inferred_object_evidence),
        catalog_column_count=len(catalog.model_inferred_column_evidence),
        catalog_fingerprint=catalog.fingerprint,
        warnings=generation_result.warnings, errors=generation_result.errors, error=None,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, catalog_build_duration_seconds=catalog_duration,
        index_preparation_duration_seconds=index_duration, total_duration_seconds=total_duration,
    )
