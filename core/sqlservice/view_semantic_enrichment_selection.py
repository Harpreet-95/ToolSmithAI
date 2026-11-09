"""
Deterministic VIEW-ONLY candidate selection for deep semantic enrichment —
SQL Architecture Correction, Phase 6, Step 6C, Part 2.

Reuses the EXACT tier-classification boundary core.sqlservice.
retrieval_documents.classify_retrieval_tier already computes for every
document (Tier 1/2 == eligible business-facing objects; Tier 3/4 ==
system/archive/backup/staging/temporary/test/deprecated, already excluded
upstream by that classifier's own stale-copy-token/system-schema logic — see
that module's own docstring). This module adds exactly one additional
filter classify_retrieval_tier does not apply: `object_type == "VIEW"`. It
never re-implements schema-exclusion or naming-token logic itself — doing
so would be a second, potentially-diverging copy of a policy that already
exists and is already tested.

Pure and in-memory: no I/O, no provider call, no database access. The
"already-current cached enrichment" / "missing or stale" distinction (Part
2's own requirement) is NOT computed here — it genuinely requires the full
bounded evidence input (SQL definition, dependency edges — see
core.sqlservice.view_semantic_enrichment_input) to compute a view's cache
key (core.sqlservice.semantic_enrichment_service.compute_view_cache_key),
which this module's pure ELIGIBILITY filter does not build. That comparison
is performed end-to-end by
core.sqlservice.semantic_enrichment_service.generate_view_semantic_enrichment
itself (its own `already_cached_keys` parameter), exactly mirroring how
generate_object_core_semantic_enrichment's OWN tier-filter
(build_object_semantic_inputs) and its OWN already-cached comparison are
two separate, composed steps rather than one entangled one. This module's
job ends at "which object_fqns are eligible, in what order, bounded by what
maximum" — Part 2's remaining bullets in full.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.config import SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS

__all__ = [
    "OBJECT_TYPE_VIEW",
    "EXCLUSION_REASON_NOT_A_VIEW",
    "EXCLUSION_REASON_INELIGIBLE_TIER",
    "ViewEnrichmentCandidate",
    "ExcludedView",
    "ViewSelectionResult",
    "select_view_enrichment_candidates",
]

OBJECT_TYPE_VIEW = "VIEW"

EXCLUSION_REASON_NOT_A_VIEW = "NOT_A_VIEW"
# Deliberately the SAME reason label core.sqlservice.semantic_enrichment_
# service.SKIP_REASON_INELIGIBLE_TIER already uses for tables -- never a
# second, differently-spelled synonym for the identical concept.
EXCLUSION_REASON_INELIGIBLE_TIER = "INELIGIBLE_TIER"


@dataclass(frozen=True)
class ViewEnrichmentCandidate:
    """One eligible view — canonical FQN preserved exactly as the source
    document carries it (never re-cased, re-quoted, or otherwise
    normalized)."""

    object_fqn: str
    schema_name: str
    view_name: str
    retrieval_tier: int


@dataclass(frozen=True)
class ExcludedView:
    """One document considered but NOT selected, and why. Never a silent
    drop — every document in the input either becomes a ViewEnrichmentCandidate,
    an ExcludedView, or a `truncated_object_fqns` entry (mutually exclusive,
    covering every input document exactly once)."""

    object_fqn: str
    reason: str


@dataclass(frozen=True)
class ViewSelectionResult:
    """Immutable, deterministic selection result. `candidates` is sorted by
    `object_fqn` (case-insensitive, canonical FQN preserved) — the SAME
    deterministic ordering core.sqlservice.semantic_enrichment_service's own
    generate_view_semantic_enrichment/its batch planner re-derive
    independently (both sort by `object_fqn.lower()`), so this module's
    ordering is never the only thing a caller can rely on, but it is always
    consistent with it.

    `total_documents`/`eligible_view_count`/`excluded_count`/
    `selected_count`/`truncated_count` are provided for observability (the
    Part 10 dry-run report reads these directly) — always internally
    consistent: `eligible_view_count == selected_count + truncated_count`,
    and `total_documents == eligible_view_count + excluded_count`."""

    candidates: tuple[ViewEnrichmentCandidate, ...]
    excluded: tuple[ExcludedView, ...]
    truncated_object_fqns: tuple[str, ...]
    warnings: tuple[str, ...]
    total_documents: int
    eligible_view_count: int
    excluded_count: int
    selected_count: int
    truncated_count: int


def select_view_enrichment_candidates(
    documents: Sequence,
    *,
    max_total_views: int = SQL_SEMANTIC_ENRICHMENT_VIEW_MAX_TOTAL_VIEWS,
) -> ViewSelectionResult:
    """
    Deterministically select every eligible (object_type == "VIEW",
    retrieval_tier in {1, 2}) `document` from `documents` — any object
    exposing `.object_fqn`/`.object_type`/`.retrieval_tier`/`.metadata`
    (the same duck-typed contract core.sqlservice.semantic_catalog.
    build_semantic_catalog and core.sqlservice.semantic_enrichment_service.
    build_object_semantic_inputs already accept — RetrievalDocument,
    IndexedSchemaDocument, and StoredRetrievalDocument all satisfy it).

    Never selects or reports on a TABLE document at all — a TABLE is simply
    absent from every output field (not even counted in `excluded`, which
    covers only VIEW documents that failed the TIER filter — counting every
    TABLE as an "excluded view" would be misleading noise in the Part 10
    report). `total_documents` counts only VIEW-typed documents considered.

    Ordering: `candidates` is sorted by `object_fqn.lower()` (ties broken by
    the original `object_fqn` string) — fully deterministic regardless of
    `documents`' own iteration order.

    Bounding: when more than `max_total_views` views are eligible, the
    FIRST `max_total_views` in canonical sorted order are kept as
    `candidates`; the rest are reported in `truncated_object_fqns` (also
    sorted) and via a warning — never silently dropped.

    Raises ValueError for `max_total_views <= 0`.
    """
    if not isinstance(max_total_views, int) or isinstance(max_total_views, bool) or max_total_views <= 0:
        raise ValueError(f"max_total_views must be a positive integer, got {max_total_views!r}")

    eligible: list[ViewEnrichmentCandidate] = []
    excluded: list[ExcludedView] = []
    total_documents = 0

    for doc in documents:
        object_type = getattr(doc, "object_type", None)
        if not isinstance(object_type, str) or object_type.upper() != OBJECT_TYPE_VIEW:
            continue  # not a view at all -- never counted, never reported
        total_documents += 1

        retrieval_tier = getattr(doc, "retrieval_tier", None)
        if retrieval_tier not in (1, 2):
            excluded.append(ExcludedView(object_fqn=doc.object_fqn, reason=EXCLUSION_REASON_INELIGIBLE_TIER))
            continue

        metadata = getattr(doc, "metadata", None) or {}
        schema_name = str(metadata.get("schema_name") or "")
        view_name = str(metadata.get("table_name") or metadata.get("object_name") or "")
        if not schema_name or not view_name:
            # Deterministic fallback: derive from the canonical FQN itself
            # (schema.view) rather than leaving either field blank -- never
            # invented, purely a structural split of the object's own name.
            parts = doc.object_fqn.split(".")
            if not schema_name and len(parts) >= 2:
                schema_name = parts[-2]
            if not view_name:
                view_name = parts[-1]

        eligible.append(
            ViewEnrichmentCandidate(
                object_fqn=doc.object_fqn, schema_name=schema_name, view_name=view_name,
                retrieval_tier=retrieval_tier,
            )
        )

    eligible.sort(key=lambda c: (c.object_fqn.lower(), c.object_fqn))
    excluded.sort(key=lambda e: (e.object_fqn.lower(), e.object_fqn))

    kept = eligible
    truncated: tuple[str, ...] = ()
    warnings: list[str] = []
    if len(eligible) > max_total_views:
        kept = eligible[:max_total_views]
        truncated = tuple(c.object_fqn for c in eligible[max_total_views:])
        warnings.append(f"views: {len(truncated)} item(s) truncated by max_total_views={max_total_views}")

    return ViewSelectionResult(
        candidates=tuple(kept),
        excluded=tuple(excluded),
        truncated_object_fqns=truncated,
        warnings=tuple(warnings),
        total_documents=total_documents,
        eligible_view_count=len(eligible),
        excluded_count=len(excluded),
        selected_count=len(kept),
        truncated_count=len(truncated),
    )
