"""
Bounded automatic VIEW_ENRICHED semantic-enrichment preparation coordinator
-- SQL Architecture Correction, Phase 6, Step 6E.

Mirrors core.sqlservice.semantic_enrichment_object_core_preparation's exact
orchestration shape (Step 4C2/4C2A) for the deep VIEW_ENRICHED phase: verify
ownership/READY build -> select eligible (Tier 1/2, object_type=="VIEW")
candidates -> build each candidate's bounded ViewSemanticInput from already-
loaded evidence -> compute per-view VIEW_ENRICHED cache keys -> generate
(bounded-concurrency) only what's missing -> persist every strictly
validated result -> report a typed VIEW_ENRICHED readiness status.

Deliberately does NOT merge into a SemanticCatalog or warm the linker/join-
planner index caches itself -- Step 6E's own scope is VIEW_ENRICHED
generation/persistence/readiness/diagnostics only (see
core.sqlservice.view_semantic_enrichment_catalog_adapter for the still-
unwired catalog merge path, and core.sqlservice.semantic_catalog for the
in-memory rebuild Part 4 exercises separately).

Never calls a real provider itself (the caller supplies a provider_factory),
never runs schema discovery, never connects to a live SQL Server, never
generates or executes SQL, and is not wired into any route or rollout flag.
Never logs provider exception text, a raw provider payload, a prompt, a
credential, or row data -- only structural identifiers, counts, and the
same bounded ViewValidationDiagnostic (code + field_path) core.sqlservice.
semantic_enrichment_service already produces for a validation failure.

Failure policy (mirrors Step 4C2A Problem 1 exactly, VIEW_ENRICHED-shaped):
generate_view_semantic_enrichment() never raises for a per-batch failure --
every batch's outcome (success, or one of a small BATCH_FAILURE_* category,
plus a ViewValidationDiagnostic when that category is a validation failure)
is collected independently. This coordinator persists every SUCCESSFUL
batch's views regardless of what happened to any OTHER batch. Only a
genuinely PRE-DISPATCH failure (an invalid bound, or the one-time provider-
factory identity probe failing before any batch is ever sent) still
propagates and aborts the entire call with zero provider calls and zero
writes.

Transaction policy: ONE VIEW = ONE ATOMIC TRANSACTION. core.sqlservice.
semantic_enrichment_store.insert_view_semantic_enrichment() already commits
(or rolls back) each view's row independently -- this coordinator does not
introduce a coarser, multi-view transaction. An unexpected exception from
ONE view's persist call is caught HERE, that view is marked
PERSISTENCE_FAILED (and therefore correctly reported as still-missing by
the next readiness check), and persistence CONTINUES for every other view.

Resumability is achieved the SAME way object_core's coordinator achieves
it: this function is itself a single, idempotent pass with no internal
retry loop. "At most one bounded resume" (Step 6E, Parts 2/3) is a
CALL-SITE policy -- call this function once, and if its status is
STATUS_VIEW_ENRICHED_PARTIAL a caller MAY call it exactly one more time;
this module never loops internally and never makes that decision itself.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from core.config import (
    SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OUTPUT_TOKENS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS,
    SQL_SEMANTIC_ENRICHMENT_VIEW_TIMEOUT_SECONDS,
)
from core.sqlservice.retrieval_store import (
    StoredRetrievalBuild,
    get_latest_ready_retrieval_build,
    load_stored_retrieval_documents,
)
from core.sqlservice.semantic_enrichment_phases import (
    PHASE_VIEW_ENRICHED,
    STATUS_NOT_PREPARED,
    STATUS_VIEW_ENRICHED_PARTIAL,
    STATUS_VIEW_ENRICHED_READY,
    readiness_status,
)
from core.sqlservice.semantic_enrichment_service import (
    BATCH_FAILURE_PROVIDER_CALL_FAILED,
    BATCH_FAILURE_PROVIDER_FACTORY_FAILED,
    BATCH_FAILURE_UNEXPECTED_WORKER_ERROR,
    BATCH_FAILURE_VALIDATION_FAILED,
    DIAGNOSTIC_ACTION_BATCH_REJECTED,
    DIAGNOSTIC_ACTION_CLAIM_DROPPED,
    DIAGNOSTIC_ACTION_NORMALIZED,
    DIAGNOSTIC_ACTION_VIEW_REJECTED,
    VIEW_ENRICHMENT_CONTRACT_VERSION,
    ColumnSemanticInput,
    SKIP_REASON_INPUT_TOO_LARGE,
    SKIP_REASON_OUTPUT_BUDGET_EXCEEDED,
    SemanticEnrichmentCacheKey,
    SemanticEnrichmentConfigurationError,
    SemanticEnrichmentProvider,
    SemanticEnrichmentProviderError,
    SemanticEnrichmentProviderFactoryError,
    ViewValidationDiagnostic,
    compute_view_cache_key,
    generate_view_semantic_enrichment,
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
    count_view_enrichment_completeness,
    insert_view_semantic_enrichment,
)
from core.sqlservice.view_semantic_enrichment_input import RawDependencyEdge, build_view_semantic_input
from core.sqlservice.view_semantic_enrichment_selection import select_view_enrichment_candidates
from data.db import get_connection

logger = logging.getLogger(__name__)

__all__ = [
    "STATUS_VIEW_ENRICHED_READY",
    "STATUS_VIEW_ENRICHED_PARTIAL",
    "STATUS_NOT_PREPARED",
    "STATUS_UNOWNED",
    "PERSISTENCE_FAILURE_CATEGORY",
    "PROVIDER_LEVEL_FAILURE_CATEGORIES",
    "ViewEnrichmentPreparationResult",
    "DiagnosticCount",
    "prepare_source_view_enrichment",
    "summarize_view_validation_diagnostics",
    "build_eligible_view_inputs",
]

STATUS_UNOWNED = "UNOWNED"

PERSISTENCE_FAILURE_CATEGORY = "PERSISTENCE_FAILED"

PROVIDER_LEVEL_FAILURE_CATEGORIES = frozenset({
    BATCH_FAILURE_PROVIDER_CALL_FAILED, BATCH_FAILURE_PROVIDER_FACTORY_FAILED,
    BATCH_FAILURE_UNEXPECTED_WORKER_ERROR,
})


@dataclass(frozen=True)
class DiagnosticCount:
    """One (code, field_path) pair from one or more rejected VIEW_ENRICHED
    batches, and how many batches produced it this run. Never carries a raw
    provider value, response body, prompt, exception message, or
    credential -- see ViewValidationDiagnostic's own docstring."""

    code: str
    field_path: str
    count: int


def summarize_view_validation_diagnostics(
    diagnostics: tuple[ViewValidationDiagnostic, ...],
) -> tuple[DiagnosticCount, ...]:
    """
    Group every ViewValidationDiagnostic (batch-rejected, view-rejected,
    claim-dropped, or normalized -- Phase 6 Step 6F, Part 3) by
    (code, field_path), deterministically ordered. Pure and safe to
    include verbatim in any report -- every field ViewValidationDiagnostic
    itself carries is already sanitized.
    """
    counts: dict[tuple[str, str], int] = {}
    for diag in diagnostics:
        key = (diag.code, diag.field_path)
        counts[key] = counts.get(key, 0) + 1
    return tuple(
        DiagnosticCount(code=code, field_path=field_path, count=count)
        for (code, field_path), count in sorted(counts.items())
    )


def _count_by_action(diagnostics: tuple[ViewValidationDiagnostic, ...], action: str) -> int:
    return sum(1 for d in diagnostics if d.action == action)


@dataclass(frozen=True)
class ViewEnrichmentPreparationResult:
    """Result of one prepare_source_view_enrichment() call. Mirrors
    ObjectCoreEnrichmentPreparationResult's field shape and meanings
    exactly (see that dataclass's own docstring), VIEW_ENRICHED-specific
    additions only: `validation_diagnostics` (deduplicated, grouped) and
    `oversized_view_fqns` (Step 6C1 Part 3's per-view output-budget skip,
    which OBJECT_CORE has no equivalent of)."""

    status: str
    source_id: int
    build_id: int | None
    schema_fingerprint: str | None
    provider_id: str
    model: str
    contract_version: int
    eligible_view_count: int
    reused_count: int
    generated_count: int
    repaired_count: int
    cached_view_count: int
    newly_persisted_view_count: int
    skipped_count: int
    oversized_view_fqns: tuple[str, ...]
    batch_validation_failed_count: int
    dispatched_batch_count: int
    successful_provider_batch_count: int
    failed_provider_batch_count: int
    validated_batch_count: int
    persisted_batch_count: int
    batch_count: int
    concurrency_used: int
    missing_view_count: int
    missing_view_fqns: tuple[str, ...]
    failure_categories_by_batch_id: Mapping[int, str]
    validation_diagnostics: tuple[DiagnosticCount, ...]
    normalized_count: int
    claim_dropped_count: int
    view_rejected_count: int
    batch_rejected_count: int
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
    eligible_view_count: int = 0, reused_count: int = 0, generated_count: int = 0, repaired_count: int = 0,
    skipped_count: int = 0, oversized_view_fqns: tuple[str, ...] = (), batch_validation_failed_count: int = 0,
    dispatched_batch_count: int = 0, successful_provider_batch_count: int = 0,
    failed_provider_batch_count: int = 0, validated_batch_count: int = 0, persisted_batch_count: int = 0,
    concurrency_used: int = 0, missing_view_fqns: tuple[str, ...] = (),
    failure_categories_by_batch_id: Mapping[int, str] | None = None,
    validation_diagnostics: tuple[DiagnosticCount, ...] = (),
    normalized_count: int = 0, claim_dropped_count: int = 0,
    view_rejected_count: int = 0, batch_rejected_count: int = 0,
    warnings: tuple[str, ...] = (), error: str | None = None,
    lookup_duration: float = 0.0, generation_duration: float = 0.0, persistence_duration: float = 0.0,
    total_duration: float = 0.0,
) -> ViewEnrichmentPreparationResult:
    return ViewEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id=provider_id, model=model, contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        eligible_view_count=eligible_view_count, reused_count=reused_count, generated_count=generated_count,
        repaired_count=repaired_count, cached_view_count=reused_count,
        newly_persisted_view_count=generated_count + repaired_count,
        skipped_count=skipped_count, oversized_view_fqns=oversized_view_fqns,
        batch_validation_failed_count=batch_validation_failed_count,
        dispatched_batch_count=dispatched_batch_count, successful_provider_batch_count=successful_provider_batch_count,
        failed_provider_batch_count=failed_provider_batch_count, validated_batch_count=validated_batch_count,
        persisted_batch_count=persisted_batch_count, batch_count=dispatched_batch_count,
        concurrency_used=concurrency_used, missing_view_count=len(missing_view_fqns),
        missing_view_fqns=missing_view_fqns,
        failure_categories_by_batch_id=dict(failure_categories_by_batch_id or {}),
        validation_diagnostics=validation_diagnostics,
        normalized_count=normalized_count, claim_dropped_count=claim_dropped_count,
        view_rejected_count=view_rejected_count, batch_rejected_count=batch_rejected_count,
        resumable=(status == STATUS_VIEW_ENRICHED_PARTIAL),
        warnings=warnings, errors=(), error=error,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, total_duration_seconds=total_duration,
    )


def _probe_provider_identity(provider_factory: Callable[[], SemanticEnrichmentProvider]) -> tuple[str, str]:
    """One factory call, purely to read (provider_id, model) -- needed for
    this coordinator's OWN cache lookups before generation ever runs. A
    broken factory here means ZERO provider calls and zero writes happen at
    all."""
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


def _column_semantic_inputs_from_metadata(metadata: Mapping) -> tuple[ColumnSemanticInput, ...]:
    columns = []
    for descriptor in metadata.get("column_descriptors") or ():
        if not isinstance(descriptor, Mapping):
            continue
        name = descriptor.get("column_name")
        if not isinstance(name, str) or not name:
            continue
        columns.append(
            ColumnSemanticInput(
                column_name=name,
                normalized_sql_type=str(descriptor.get("normalized_sql_type") or "OTHER"),
                is_primary_key=bool(descriptor.get("is_primary_key")),
                is_foreign_key=bool(descriptor.get("is_foreign_key")),
            )
        )
    if columns:
        return tuple(sorted(columns, key=lambda c: c.column_name.lower()))
    primary_keys = set(metadata.get("primary_key_columns") or ())
    return tuple(
        sorted(
            (ColumnSemanticInput(name, "OTHER", name in primary_keys, False) for name in (metadata.get("column_names") or ())),
            key=lambda c: c.column_name.lower(),
        )
    )


def _load_view_definition(conn, source_id: int, object_fqn: str) -> str | None:
    row = conn.execute(
        "SELECT definition_sql FROM view_semantic_evidence "
        "WHERE source_id = ? AND table_fqn = ? ORDER BY discovered_at DESC LIMIT 1",
        (source_id, object_fqn),
    ).fetchone()
    return row["definition_sql"] if row is not None else None


def _load_dependency_edges(conn, source_id: int, object_fqn: str) -> tuple[RawDependencyEdge, ...]:
    rows = conn.execute(
        "SELECT from_table_fqn, to_table_fqn, relationship_type, inference_method, relationship_status "
        "FROM table_relationships WHERE source_id = ? AND (from_table_fqn = ? OR to_table_fqn = ?)",
        (source_id, object_fqn, object_fqn),
    ).fetchall()
    edges = []
    for row in rows:
        other = row["to_table_fqn"] if row["from_table_fqn"] == object_fqn else row["from_table_fqn"]
        edges.append(
            RawDependencyEdge(
                referenced_object_fqn=other,
                relationship_type=row["relationship_type"],
                inference_method=row["inference_method"],
                relationship_status=row["relationship_status"],
            )
        )
    return tuple(edges)


def build_eligible_view_inputs(source_id: int, documents):
    """Selection (Tier 1/2, VIEW-only) -> bounded ViewSemanticInput for
    every eligible candidate, reading already-discovered evidence
    (view_semantic_evidence / table_relationships) read-only. Never a live
    SQL Server call -- these are ToolSmithAI's own local metadata tables,
    the same tables development validation material (not included here)
    step6c_dry_run.py already reads for the identical purpose."""
    selection = select_view_enrichment_candidates(documents)
    doc_by_fqn = {d.object_fqn: d for d in documents}

    conn = get_connection()
    try:
        view_inputs = []
        for candidate in selection.candidates:
            doc = doc_by_fqn.get(candidate.object_fqn)
            metadata = dict(doc.metadata) if doc is not None else {}
            columns = _column_semantic_inputs_from_metadata(metadata)
            raw_definition = _load_view_definition(conn, source_id, candidate.object_fqn)
            raw_edges = _load_dependency_edges(conn, source_id, candidate.object_fqn)
            build_result = build_view_semantic_input(
                candidate,
                columns=columns,
                primary_key_columns=metadata.get("primary_key_columns") or (),
                dictionary_business_name=metadata.get("business_name"),
                dictionary_description=metadata.get("description"),
                dictionary_synonyms=tuple(metadata.get("synonyms") or ()),
                raw_definition_sql=raw_definition,
                dependency_edges=raw_edges,
                confirmed_relationship_summary=(),
            )
            view_inputs.append(build_result.view_input)
    finally:
        conn.close()
    return view_inputs


def prepare_source_view_enrichment(
    source_id: int,
    user_id: str,
    provider_factory: Callable[[], SemanticEnrichmentProvider],
    *,
    max_objects_per_batch: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OBJECTS_PER_BATCH,
    max_batch_characters: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_BATCH_CHARACTERS,
    max_total_objects: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS,
    max_estimated_output_tokens: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_ESTIMATED_OUTPUT_TOKENS,
    max_output_tokens: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_OUTPUT_TOKENS,
    timeout_seconds: int = SQL_SEMANTIC_ENRICHMENT_VIEW_TIMEOUT_SECONDS,
    max_concurrency: int = SQL_SEMANTIC_ENRICHMENT_VIEW_DEFAULT_CONCURRENCY,
    view_inputs_override: tuple | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> ViewEnrichmentPreparationResult:
    """
    Prepare one source's VIEW_ENRICHED semantic enrichment end to end --
    see this module's own docstring for the complete failure/transaction/
    resumability policy (identical shape to
    prepare_source_object_core_enrichment, VIEW_ENRICHED-specific fields
    only).

    `view_inputs_override`, when given, REPLACES the normal "select every
    eligible Tier 1/2 view" step with exactly this caller-supplied set of
    ViewSemanticInput -- used ONLY to scope a run to a specific subset (a
    pilot resume over a handful of named views); every other stage
    (cache-key lookup, generation, persistence, readiness) is completely
    unchanged. `eligible_view_count` still reports the override's own size
    in this mode -- never the full-source count -- since "eligible" here
    means "eligible for THIS call".
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

    lookup_start = clock()
    if view_inputs_override is not None:
        view_inputs = list(view_inputs_override)
    else:
        stored_documents = load_stored_retrieval_documents(build.id, source_id, user_id)
        view_inputs = build_eligible_view_inputs(source_id, stored_documents)
    eligible_view_count = len(view_inputs)

    cache_keys_by_fqn: dict[str, SemanticEnrichmentCacheKey] = {
        vi.object_fqn: compute_view_cache_key(vi, provider_id, model) for vi in view_inputs
    }
    current_hashes_by_object_fqn = {fqn: key.semantic_input_hash for fqn, key in cache_keys_by_fqn.items()}

    completeness_before = count_view_enrichment_completeness(
        source_id, user_id, provider_id=provider_id, model=model,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn,
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

    generation_start = clock()
    generation_result = generate_view_semantic_enrichment(
        view_inputs, provider_factory, already_cached_keys=already_cached_keys,
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

    persistence_start = clock()
    generated_count = 0
    repaired_count = 0
    reused_after_generation = 0
    persistence_failed_fqns: set[str] = set()
    for obj in generation_result.enriched_views:
        cache_key = generation_result.cache_keys_by_object_fqn[obj.object_fqn]
        cols = [c for c in generation_result.enriched_columns if c.object_fqn == obj.object_fqn]
        try:
            insert_result = insert_view_semantic_enrichment(
                user_id, source_id=source_id, object_fqn=obj.object_fqn,
                semantic_input_hash=cache_key.semantic_input_hash,
                provider_id=provider_id, model=model,
                contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
                object_semantics=obj, column_semantics=cols,
            )
        except Exception as exc:  # noqa: BLE001 - isolate to this ONE view; never abort the loop
            persistence_failed_fqns.add(obj.object_fqn)
            logger.warning(
                "VIEW_ENRICHED persistence failed for one view under source_id=%s build_id=%s "
                "(%s) -- view remains missing and will be retried on the next call",
                source_id, build.id, type(exc).__name__,
            )
            continue

        if insert_result.outcome == _STORE_OUTCOME_UNOWNED:
            persistence_duration = _elapsed(persistence_start)
            logger.warning(
                "VIEW_ENRICHED persistence reported UNOWNED mid-run for source_id=%s build_id=%s",
                source_id, build.id,
            )
            return _empty_result(
                STATUS_UNOWNED, source_id, provider_id, model, build_id=build.id,
                schema_fingerprint=build.schema_fingerprint, eligible_view_count=eligible_view_count,
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

    completeness_after = count_view_enrichment_completeness(
        source_id, user_id, provider_id=provider_id, model=model,
        contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        current_hashes_by_object_fqn=current_hashes_by_object_fqn,
    )
    missing_view_fqns = completeness_after.missing_object_fqns
    status = readiness_status(
        PHASE_VIEW_ENRICHED, eligible_count=eligible_view_count, missing_count=len(missing_view_fqns),
    )

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
    validation_diagnostics = summarize_view_validation_diagnostics(generation_result.diagnostics)
    normalized_count = _count_by_action(generation_result.diagnostics, DIAGNOSTIC_ACTION_NORMALIZED)
    claim_dropped_count = _count_by_action(generation_result.diagnostics, DIAGNOSTIC_ACTION_CLAIM_DROPPED)
    view_rejected_count = _count_by_action(generation_result.diagnostics, DIAGNOSTIC_ACTION_VIEW_REJECTED)
    batch_rejected_count = _count_by_action(generation_result.diagnostics, DIAGNOSTIC_ACTION_BATCH_REJECTED)
    # Only the genuinely oversized (pre-dispatch, never even sent to the
    # provider) skip reasons -- generation_result.skipped_objects ALSO
    # carries SKIP_REASON_BATCH_VALIDATION_FAILED/PARTIAL_COVERAGE entries
    # for views whose batch WAS dispatched and failed strict validation;
    # those are already fully accounted for via missing_view_fqns/
    # failure_categories_by_batch_id/validation_diagnostics above and must
    # never be double-counted here as "oversized".
    _OVERSIZED_SKIP_REASONS = frozenset({SKIP_REASON_INPUT_TOO_LARGE, SKIP_REASON_OUTPUT_BUDGET_EXCEEDED})
    oversized_view_fqns = tuple(sorted(
        s.object_fqn for s in generation_result.skipped_objects if s.reason in _OVERSIZED_SKIP_REASONS
    ))

    total_duration = _elapsed(total_start)

    logger.info(
        "VIEW_ENRICHED preparation completed for source_id=%s build_id=%s status=%s: "
        "eligible=%s cached=%s newly_persisted=%s missing=%s dispatched_batches=%s validated_batches=%s "
        "persisted_batches=%s failed_batches=%s concurrency=%s",
        source_id, build.id, status, eligible_view_count,
        reused_before_generation + reused_after_generation, generated_count + repaired_count,
        len(missing_view_fqns), generation_result.batch_count, validated_batch_count,
        persisted_batch_count, len(failure_categories_by_batch_id), generation_result.concurrency_used,
    )

    return ViewEnrichmentPreparationResult(
        status=status, source_id=source_id, build_id=build.id, schema_fingerprint=build.schema_fingerprint,
        provider_id=provider_id, model=model, contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION,
        eligible_view_count=eligible_view_count,
        reused_count=reused_before_generation + reused_after_generation,
        generated_count=generated_count, repaired_count=repaired_count,
        cached_view_count=reused_before_generation + reused_after_generation,
        newly_persisted_view_count=generated_count + repaired_count,
        skipped_count=generation_result.skipped_count, oversized_view_fqns=oversized_view_fqns,
        batch_validation_failed_count=batch_validation_failed_count,
        dispatched_batch_count=generation_result.batch_count,
        successful_provider_batch_count=successful_provider_batch_count,
        failed_provider_batch_count=failed_provider_batch_count,
        validated_batch_count=validated_batch_count, persisted_batch_count=persisted_batch_count,
        batch_count=generation_result.batch_count, concurrency_used=generation_result.concurrency_used,
        missing_view_count=len(missing_view_fqns), missing_view_fqns=missing_view_fqns,
        failure_categories_by_batch_id=dict(failure_categories_by_batch_id),
        validation_diagnostics=validation_diagnostics,
        normalized_count=normalized_count, claim_dropped_count=claim_dropped_count,
        view_rejected_count=view_rejected_count, batch_rejected_count=batch_rejected_count,
        resumable=(status == STATUS_VIEW_ENRICHED_PARTIAL),
        warnings=generation_result.warnings, errors=generation_result.errors, error=None,
        lookup_duration_seconds=lookup_duration, generation_duration_seconds=generation_duration,
        persistence_duration_seconds=persistence_duration, total_duration_seconds=total_duration,
    )
