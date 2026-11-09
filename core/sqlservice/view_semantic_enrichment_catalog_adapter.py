"""
Pure adapter: validated VIEW_ENRICHED enrichment -> core.sqlservice.
semantic_catalog.build_semantic_catalog's existing `enrichment_input` shape
— SQL Architecture Correction, Phase 6, Step 6C, Part 7; wired into
core.sqlservice.schema_plan_options.py's own `_catalog_for` as of Phase 6
Step 6F, Part 4 ("the business semantics generated at connection time
actually influence runtime schema selection").

Two entry points for the SAME transform, over two different input shapes:
adapt_view_enrichment_to_catalog_input() for a fresh, same-process
ViewEnrichmentResult (EnrichedViewObjectSemantics/EnrichedColumnSemantics,
matched by object_fqn since EnrichedColumnSemantics carries one), and
adapt_stored_view_enrichment_to_catalog_input() for rows reloaded from the
store (StoredViewSemanticEnrichment, whose own `.columns` are already
correctly scoped -- StoredEnrichedColumn carries no object_fqn at all,
by design, since it never needs one there).

Why no new SemanticCatalog code is needed at all: build_semantic_catalog's
`enrichment_input` parameter is ALREADY duck-typed to the exact shape
core.sqlservice.semantic_enrichment_store.StoredSemanticEnrichment exposes
(object_fqn, business_name, description, object_role, entity_concepts,
aliases, confidence, provenance, and a `.columns` sequence of column_name/
business_label/semantic_role/date_role/measure_role/dimension_role/
identifier_role/confidence/provenance) — see that function's own docstring:
"`enrichment_input`... is duck-typed the same way `documents` itself is...
NOT imported here, to avoid a circular dependency". This module's ENTIRE
job is therefore just building one small wrapper object per view exposing
those exact attribute names (`business_domain`/`status_concepts`/
`source_role`/`supported_operations` are simply extra attributes
_build_model_inferred_evidence never reads via getattr — harmless, never
rejected, never a "no additional fields" contract to violate, since that
strict-shape rule only ever applies to a PROVIDER response, never to a
Python object's own attribute set).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.sqlservice.semantic_catalog import EvidenceItem
from core.sqlservice.semantic_enrichment_service import (
    EnrichedColumnSemantics,
    EnrichedViewObjectSemantics,
    ViewStatusConceptRef,
)

__all__ = [
    "ViewCatalogEnrichmentColumn",
    "ViewCatalogEnrichmentEntry",
    "adapt_view_enrichment_to_catalog_input",
    "adapt_stored_view_enrichment_to_catalog_input",
]


@dataclass(frozen=True)
class ViewCatalogEnrichmentColumn:
    """One column's semantics, in the exact attribute shape
    build_semantic_catalog._build_model_inferred_evidence reads via getattr
    (column_name/business_label/semantic_role/date_role/measure_role/
    dimension_role/identifier_role/confidence/provenance) — a plain,
    duck-typed value object; never a new SemanticCatalog concept."""

    column_name: str
    business_label: str
    semantic_role: str
    date_role: str | None
    measure_role: str | None
    dimension_role: str | None
    identifier_role: str | None
    confidence: float
    provenance: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class ViewCatalogEnrichmentEntry:
    """One view's complete duck-typed `enrichment_input` entry — the exact
    attribute shape build_semantic_catalog's own docstring documents
    (object_fqn/business_name/description/object_role/entity_concepts/
    aliases/confidence/provenance/columns). `object_role` is populated from
    EnrichedViewObjectSemantics.source_role (the SAME attribute name
    build_semantic_catalog._build_model_inferred_evidence already reads for
    every TABLE OBJECT_CORE/COLUMN_DEFERRED entry — a view's source role
    becomes ModelInferredObjectEvidence.object_role_candidates evidence
    exactly like a table's structural object_role would, never a new
    catalog field).

    `business_domain`/`status_concepts`/`supported_operations` (Phase 6,
    Step 6F, Part 4) ARE now carried through — build_semantic_catalog's
    own `_build_model_inferred_evidence` reads them (via getattr, so a
    table-level entry lacking these attributes is unaffected) into its own
    `business_domain_candidates`/`status_concept_label_candidates`/
    `supported_operation_candidates` fields. `status_concepts` is the
    SAME `ViewStatusConceptRef` tuple EnrichedViewObjectSemantics itself
    carries — never re-wrapped."""

    object_fqn: str
    business_name: str
    description: str
    object_role: str
    entity_concepts: tuple[str, ...]
    aliases: tuple[str, ...]
    confidence: float
    provenance: tuple[EvidenceItem, ...]
    columns: tuple[ViewCatalogEnrichmentColumn, ...]
    business_domain: str = ""
    status_concepts: tuple[ViewStatusConceptRef, ...] = ()
    supported_operations: tuple[str, ...] = ()


def adapt_view_enrichment_to_catalog_input(
    enriched_views: Sequence[EnrichedViewObjectSemantics],
    enriched_columns: Sequence[EnrichedColumnSemantics],
) -> tuple[ViewCatalogEnrichmentEntry, ...]:
    """
    Pure transform, deterministic and order-preserving-by-input: pairs each
    `EnrichedViewObjectSemantics` with its own `EnrichedColumnSemantics`
    (matched by `object_fqn`, case-insensitively) and returns a tuple ready
    to pass directly as `build_semantic_catalog(documents,
    enrichment_input=adapt_view_enrichment_to_catalog_input(...))`.

    Never invents an object or column: every returned entry's `object_fqn`
    and every returned column's `column_name` come verbatim from an
    already-validated EnrichedViewObjectSemantics/EnrichedColumnSemantics
    instance (both constructible only via core.sqlservice.
    semantic_enrichment_service's own construction guard) — this function
    performs no independent validation of its own because none is needed;
    build_semantic_catalog's own `_build_model_inferred_evidence` re-checks
    every object_fqn/column_name against REAL structure in `documents`
    regardless (its own "model inference may not create objects or
    columns" requirement 4), so this adapter's output is advisory input to
    an already-defensive consumer, never a trusted bypass of it.

    `status_concepts`/`business_domain`/`supported_operations` (Phase 6,
    Step 6F, Part 4) ARE carried onto ViewCatalogEnrichmentEntry --
    ModelInferredObjectEvidence gained matching candidate fields for
    exactly this purpose. Still never structural: none of the three ever
    changes `entities`/`object_families`/any other structural catalog
    field; a status concept's own column reference was already validated
    against real structure at generation time (the contract's own "column
    MUST resolve to a real column" rule).
    """
    columns_by_fqn_lower: dict[str, list[ViewCatalogEnrichmentColumn]] = {}
    for col in enriched_columns:
        columns_by_fqn_lower.setdefault(col.object_fqn.lower(), []).append(
            ViewCatalogEnrichmentColumn(
                column_name=col.column_name,
                business_label=col.business_label,
                semantic_role=col.semantic_role,
                date_role=col.date_role,
                measure_role=col.measure_role,
                dimension_role=col.dimension_role,
                identifier_role=col.identifier_role,
                confidence=col.confidence,
                provenance=col.provenance,
            )
        )

    entries = []
    for view in enriched_views:
        entries.append(
            ViewCatalogEnrichmentEntry(
                object_fqn=view.object_fqn,
                business_name=view.business_name,
                description=view.description,
                object_role=view.source_role,
                entity_concepts=view.entity_concepts,
                aliases=view.aliases,
                confidence=view.confidence,
                provenance=view.provenance,
                columns=tuple(columns_by_fqn_lower.get(view.object_fqn.lower(), ())),
                business_domain=view.business_domain,
                status_concepts=view.status_concepts,
                supported_operations=view.supported_operations,
            )
        )
    return tuple(entries)


def adapt_stored_view_enrichment_to_catalog_input(
    stored_views: Sequence,
) -> tuple[ViewCatalogEnrichmentEntry, ...]:
    """
    Phase 6, Step 6F, Part 4 -- the STORED/reloaded counterpart of
    adapt_view_enrichment_to_catalog_input(), for
    core.sqlservice.semantic_enrichment_store.StoredViewSemanticEnrichment
    rows (e.g. from load_current_view_enrichments_for_source()) rather than
    a fresh, same-process ViewEnrichmentResult.

    Deliberately a SEPARATE function, not a "duck-typed either shape"
    branch inside adapt_view_enrichment_to_catalog_input: StoredEnrichedColumn
    (the persisted column shape) carries NO `object_fqn` field at all --
    unlike EnrichedColumnSemantics, it doesn't need one, because it is
    already correctly scoped as one entry of its OWN parent
    StoredViewSemanticEnrichment.columns tuple. Flattening every stored
    view's columns into one global list and re-grouping by a
    (non-existent) `.column.object_fqn` -- as the fresh-data function does
    for EnrichedColumnSemantics, which DOES carry that field -- is a type
    error for this shape, not merely a redundant computation. This
    function instead takes each StoredViewSemanticEnrichment's own
    `.columns` DIRECTLY, so no cross-view matching is ever needed at all.

    Same non-invention guarantee as adapt_view_enrichment_to_catalog_input:
    every field comes verbatim from an already strictly-validated,
    persisted row; build_semantic_catalog's own `_build_model_inferred_
    evidence` still independently re-checks every object_fqn/column_name
    against real structure regardless.
    """
    entries = []
    for view in stored_views:
        entries.append(
            ViewCatalogEnrichmentEntry(
                object_fqn=view.object_fqn,
                business_name=view.business_name,
                description=view.description,
                object_role=view.source_role,
                entity_concepts=view.entity_concepts,
                aliases=view.aliases,
                confidence=view.confidence,
                provenance=view.provenance,
                columns=tuple(
                    ViewCatalogEnrichmentColumn(
                        column_name=col.column_name,
                        business_label=col.business_label,
                        semantic_role=col.semantic_role,
                        date_role=col.date_role,
                        measure_role=col.measure_role,
                        dimension_role=col.dimension_role,
                        identifier_role=col.identifier_role,
                        confidence=col.confidence,
                        provenance=col.provenance,
                    )
                    for col in view.columns
                ),
                business_domain=view.business_domain,
                status_concepts=view.status_concepts,
                supported_operations=view.supported_operations,
            )
        )
    return tuple(entries)
