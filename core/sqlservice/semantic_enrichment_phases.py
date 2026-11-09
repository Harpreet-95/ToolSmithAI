"""
Shared enrichment-phase and readiness vocabulary -- SQL Architecture
Correction, Phase 6, Step 4C2.

A single, dependency-free module (imports nothing from any other
core.sqlservice.semantic_enrichment_* module, and nothing those modules
don't already import) so both the OBJECT_CORE generation/preparation code
and the persistence store can share the same phase identity constants
without risking a circular import (core.sqlservice.semantic_enrichment_store
imports core.sqlservice.semantic_enrichment_service; a phase module either
of them needs must sit below both).

Three independent enrichment phases exist as of Phase 6 Step 6C:
  - PHASE_OBJECT_CORE: a compact, object-level-only record (label, role,
    concepts, aliases, description) for every eligible object -- required
    for schema selection (core.sqlservice.schema_linker's advisory
    candidate index) and designed to finish in minutes, not hours.
  - PHASE_COLUMN_DEFERRED: the pre-existing (Step 4A/4B/4C1) combined
    object+selected-columns contract, UNCHANGED, now explicitly tagged by
    phase so its cache rows can never collide with an OBJECT_CORE row for
    the same object -- see core.sqlservice.semantic_enrichment_service's
    SemanticEnrichmentCacheKey.phase and core.sqlservice.
    semantic_enrichment_store's own phase-aware queries. Nothing in Step
    4C2 makes a COLUMN_DEFERRED provider call; this constant exists so the
    phase DIMENSION is real and tested today, ready for a future step's
    column-enrichment coordinator to use unchanged.
  - PHASE_VIEW_ENRICHED (Phase 6 Step 6C, persistence redesigned Step 6C1):
    a deep, view-only business semantics record (business domain/purpose,
    primary entities, per-column measure/dimension/date/identifier roles,
    status concepts, source role, authoritative confidence) for eligible
    (Tier 1/2) VIEW objects only -- see core.sqlservice.
    view_semantic_enrichment_selection/_input and this module's own
    PHASE_VIEW_ENRICHED section of core.sqlservice.semantic_enrichment_
    service. Entirely additive: never assigned to a TABLE object.

    Unlike PHASE_OBJECT_CORE/PHASE_COLUMN_DEFERRED, this phase is NOT kept
    apart from the others by a shared `phase` column value -- Step 6C1's
    audit of the original design (which widened sql_semantic_enrichments'
    `phase` CHECK constraint via a table rebuild) replaced it with a
    dedicated table, `sql_view_semantic_enrichments` (see
    core.sqlservice.semantic_enrichment_store's own VIEW_ENRICHED section
    and migrations/017_create_view_semantic_enrichments.sql for the full
    rationale). PHASE_VIEW_ENRICHED remains a valid constant here purely as
    an in-memory identity/bookkeeping label -- every
    SemanticEnrichmentCacheKey a view produces still carries
    phase=PHASE_VIEW_ENRICHED (so `already_cached_keys` comparisons and
    readiness-status reporting work exactly like the other two phases),
    but that value is never written to any database column: row identity
    as a view record comes from the dedicated table itself, not from a
    `phase` value shared with tables.

Readiness is reported per phase, never conflated: OBJECT_CORE_READY/
OBJECT_CORE_PARTIAL, COLUMN_DEFERRED_READY/COLUMN_DEFERRED_PARTIAL, and
VIEW_ENRICHED_READY/VIEW_ENRICHED_PARTIAL are computed independently (see
`readiness_status`) -- incompleteness (or one phase never having run at
all) can never downgrade another phase's own readiness (Step 4C2
requirement 7, extended unchanged to PHASE_VIEW_ENRICHED).
"""

from __future__ import annotations

__all__ = [
    "PHASE_OBJECT_CORE",
    "PHASE_COLUMN_DEFERRED",
    "PHASE_VIEW_ENRICHED",
    "VALID_PHASES",
    "STATUS_OBJECT_CORE_READY",
    "STATUS_OBJECT_CORE_PARTIAL",
    "STATUS_COLUMN_DEFERRED_READY",
    "STATUS_COLUMN_DEFERRED_PARTIAL",
    "STATUS_VIEW_ENRICHED_READY",
    "STATUS_VIEW_ENRICHED_PARTIAL",
    "STATUS_NOT_PREPARED",
    "readiness_status",
]

PHASE_OBJECT_CORE = "OBJECT_CORE"
PHASE_COLUMN_DEFERRED = "COLUMN_DEFERRED"
PHASE_VIEW_ENRICHED = "VIEW_ENRICHED"
VALID_PHASES = frozenset({PHASE_OBJECT_CORE, PHASE_COLUMN_DEFERRED, PHASE_VIEW_ENRICHED})

STATUS_OBJECT_CORE_READY = "OBJECT_CORE_READY"
STATUS_OBJECT_CORE_PARTIAL = "OBJECT_CORE_PARTIAL"
STATUS_COLUMN_DEFERRED_READY = "COLUMN_DEFERRED_READY"
STATUS_COLUMN_DEFERRED_PARTIAL = "COLUMN_DEFERRED_PARTIAL"
STATUS_VIEW_ENRICHED_READY = "VIEW_ENRICHED_READY"
STATUS_VIEW_ENRICHED_PARTIAL = "VIEW_ENRICHED_PARTIAL"
# Shared with core.sqlservice.semantic_enrichment_preparation's own
# STATUS_NOT_PREPARED (same string, "NOT_PREPARED") -- both mean the same
# routine condition (no READY retrieval build yet) for either phase.
STATUS_NOT_PREPARED = "NOT_PREPARED"

_READY_BY_PHASE = {
    PHASE_OBJECT_CORE: STATUS_OBJECT_CORE_READY,
    PHASE_COLUMN_DEFERRED: STATUS_COLUMN_DEFERRED_READY,
    PHASE_VIEW_ENRICHED: STATUS_VIEW_ENRICHED_READY,
}
_PARTIAL_BY_PHASE = {
    PHASE_OBJECT_CORE: STATUS_OBJECT_CORE_PARTIAL,
    PHASE_COLUMN_DEFERRED: STATUS_COLUMN_DEFERRED_PARTIAL,
    PHASE_VIEW_ENRICHED: STATUS_VIEW_ENRICHED_PARTIAL,
}


def readiness_status(phase: str, *, eligible_count: int, missing_count: int) -> str:
    """
    Default readiness policy (Step 4C2 requirement 7): a phase is READY only
    when EVERY eligible object already has a current, ACTIVE enrichment row
    for that exact phase (missing_count == 0); PARTIAL otherwise, including
    the "phase never ran at all" case (missing_count == eligible_count).
    `eligible_count == 0` is reported READY (vacuously -- nothing to enrich
    is not incompleteness), mirroring core.sqlservice.embedding_store's own
    zero-document completeness convention.

    Raises ValueError for an unknown phase, or a `missing_count` that is
    negative or exceeds `eligible_count` -- a caller bug, never silently
    tolerated.
    """
    if phase not in VALID_PHASES:
        raise ValueError(f"unknown enrichment phase: {phase!r}")
    if missing_count < 0 or missing_count > eligible_count:
        raise ValueError(
            f"missing_count={missing_count!r} is inconsistent with eligible_count={eligible_count!r}"
        )
    return _READY_BY_PHASE[phase] if missing_count == 0 else _PARTIAL_BY_PHASE[phase]
