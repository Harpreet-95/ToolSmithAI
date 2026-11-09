"""
Deterministic semantic catalog — SQL Architecture Correction, Phase 6, Step 1.

Converts the structured metadata ALREADY attached to every retrieval
document (core.sqlservice.retrieval_documents' metadata contract — the same
contract core.sqlservice.lexical_index, canonical_scoring, and
context_packing already build on) into reusable, evidence-backed business
concepts: entities, measures, dimensions, date roles, identifiers, and
object families. This is groundwork for a future schema linker / join-
subgraph planner — this module does not itself link a question to a schema,
generate SQL, or rank anything against a question.

Explicitly NOT an LLM call and NOT a manual-approval system: every concept
is produced by a documented, generic, deterministic formula over already-
computed structured fields, with bounded confidence and machine-readable
evidence. No concept here is ever "approved"/"rejected" by a human — an
ambiguous concept simply keeps every plausible candidate, each with its own
confidence, rather than a workflow silently picking one.

Reuse (this module does not duplicate any of the following):
  - core.sqlservice.retrieval_documents' metadata contract for column_names,
    status_columns/date_columns/measure_columns, primary_key_columns,
    foreign_key_targets, confirmed_relationship_targets/sources/edges,
    business_name/description/domain/synonyms, and retrieval_tier — the
    ONLY structured fields this module reads. document_text is NEVER parsed
    as structure (mirrors lexical_index/context_packing's own contract).
  - core.sqlservice.canonical_scoring.compute_canonical_scores() for BOTH
    per-object canonical authority (folded into entity-candidate confidence)
    AND automatic object-family detection (name-token + relationship-context
    union-find) — object families here are a direct, unmodified reuse of
    that algorithm's grouping, never a second/conflicting family algorithm.
  - core.sqlservice.lexical_index.tokenize() for all text tokenization
    (snake_case/CamelCase splitting, singularization, stop-word removal) —
    the same tokenizer every other retrieval-facing module already uses.

Input restrictions (enforced by construction, not merely by convention):
  - No database access, no network call, no OpenAI/embedding-provider call
    anywhere in this module.
  - Only already-loaded document objects are read (object_fqn, object_type,
    retrieval_tier, metadata — the same duck-typed contract vector_index/
    lexical_index/canonical_scoring already accept), never a build_id/
    source_id/user_id used to go fetch anything.
  - No row values or sample data of any kind.

Determinism: `documents` may be supplied in any order — every internal
grouping/sort is keyed by object_fqn (or object_fqn+column_name), never by
input position, so a shuffled input list produces a byte-identical
`SemanticCatalog` (see `fingerprint`).

No single customer's specific table/column names or
production constants appear anywhere in this file — every category/keyword
set below is generic English business vocabulary, the same level of
genericness core.sqlservice.retrieval_documents/canonical_scoring/
lexical_index already hold themselves to (see each module's own
`test_no_domain_specific_tokens_in_production_module`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from core.sqlservice.canonical_scoring import (
    CanonicalScoringIdentity,
    compute_canonical_scores,
)
from core.sqlservice.lexical_index import tokenize

__all__ = [
    "SEMANTIC_CATALOG_FINGERPRINT_VERSION",
    "EVIDENCE_OBJECT_NAME_TOKEN",
    "EVIDENCE_COLUMN_NAME_TOKEN",
    "EVIDENCE_BUSINESS_NAME",
    "EVIDENCE_DESCRIPTION",
    "EVIDENCE_SYNONYM",
    "EVIDENCE_PRIMARY_KEY",
    "EVIDENCE_CONFIRMED_RELATIONSHIP",
    "EVIDENCE_STRUCTURAL_CATEGORY",
    "EVIDENCE_RETRIEVAL_TIER",
    "EVIDENCE_CANONICAL_SCORE",
    "EVIDENCE_FAMILY_GROUPING",
    "IDENTIFIER_KIND_PRIMARY_KEY",
    "IDENTIFIER_KIND_FOREIGN_KEY",
    "IDENTIFIER_KIND_BUSINESS_IDENTIFIER",
    "IDENTIFIER_KIND_TECHNICAL_IDENTIFIER",
    "UNCLASSIFIED_REASON_NO_MEANINGFUL_NAME_TOKEN",
    "UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY",
    "UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_ENTITIES",
    "EvidenceItem",
    "EntityCandidateObject",
    "BusinessEntity",
    "MeasureConcept",
    "DimensionConcept",
    "DateRoleCandidate",
    "DateRoleConcept",
    "IdentifierConcept",
    "ObjectFamily",
    "UnclassifiedObject",
    "ModelInferredObjectEvidence",
    "ModelInferredColumnEvidence",
    "SemanticCatalog",
    "MODEL_INFERRED_PROVENANCE_KIND",
    "MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP",
    "REJECTED_REASON_UNKNOWN_OBJECT",
    "REJECTED_REASON_UNKNOWN_COLUMN",
    "REJECTED_REASON_MISSING_PROVENANCE",
    "build_semantic_catalog",
]

# ── Fingerprint/versioning ─────────────────────────────────────────────────
#
# Domain separator for the fingerprint's canonical payload SHAPE (mirrors
# core.sqlservice.retrieval_store.DOCUMENT_SET_HASH_VERSION's own
# convention) — bump ONLY when the set of fields folded into the fingerprint
# changes, never for an ordinary content change (content changes already
# change the hash on their own).
SEMANTIC_CATALOG_FINGERPRINT_VERSION = 3  # bumped: Phase 6 Step 4B added model_inferred_object/column_evidence

# SQL Architecture Correction, Phase 6, Step 4B — automatic advisory merge.
# `enrichment_input` (build_semantic_catalog's own new optional parameter)
# is duck-typed, NOT a core.sqlservice.semantic_enrichment_service/_store
# import — this module has no dependency on that package (semantic_
# enrichment_service already imports THIS module for EvidenceItem, so the
# reverse import would be circular). The real caller (a future preparation
# coordinator) passes core.sqlservice.semantic_enrichment_store.
# StoredSemanticEnrichment instances directly; they already expose exactly
# the attributes read below (object_fqn, business_name, description,
# object_role, entity_concepts, aliases, supported_operations, confidence,
# provenance, columns[].column_name/business_label/semantic_role/date_role/
# measure_role/dimension_role/identifier_role/confidence/provenance) — the
# SAME "any object exposing the right attributes" contract `documents`
# itself already uses.
#
# MODEL_INFERRED_PROVENANCE_KIND intentionally duplicates the STRING VALUE
# of core.sqlservice.semantic_enrichment_service.PROVENANCE_MODEL_INFERRED_
# SEMANTIC (never imported, for the same circularity reason above) — two
# independent modules agreeing on one stable, machine-comparable string,
# the same level of coupling EVIDENCE_* constants already have with the
# rest of this file. An enrichment_input entry carrying no evidence item of
# this kind is defensively rejected (REJECTED_REASON_MISSING_PROVENANCE) —
# this module never trusts an unlabeled claim to be model-inferred.
MODEL_INFERRED_PROVENANCE_KIND = "MODEL_INFERRED_SEMANTIC"

# Defense in depth: re-clipped here independently of whatever confidence
# enrichment_input's own producer already claims to have capped — "semantic
# evidence, not structural truth" (Step 4B requirement 5) is enforced by
# THIS module too, never merely trusted from upstream. Kept numerically
# identical to core.sqlservice.semantic_enrichment_service.
# MODEL_INFERRED_CONFIDENCE_CAP by deliberate convention, not by import.
MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP = 0.50

REJECTED_REASON_UNKNOWN_OBJECT = "UNKNOWN_OBJECT"  # model inference may not create objects
REJECTED_REASON_UNKNOWN_COLUMN = "UNKNOWN_COLUMN"  # model inference may not create columns
REJECTED_REASON_MISSING_PROVENANCE = "MISSING_PROVENANCE"  # not labeled MODEL_INFERRED_SEMANTIC evidence

# Mirrors this module's own _AMBIGUITY_CONFIDENCE_MARGIN below, applied to
# competing model-proposed business names/object roles/column roles instead
# of entity/date-role candidates.
_MODEL_EVIDENCE_AMBIGUITY_MARGIN = 0.10

_ELIGIBLE_ENTITY_TIERS = frozenset({1, 2})  # Tier 3/4 never seed an entity concept
_ALL_TIERS = frozenset({1, 2, 3, 4})

# SQL Architecture Correction, Phase 6, Step 1A — schema-scaled entity
# bounds. A live audit found a FIXED max_entities=200 (the Step 1 default)
# silently truncated a thousand-plus-eligible-object schema down to 200 concepts
# with no accounting of what was lost. `max_entities`/`max_candidates_per_
# entity` now default to None, resolved per-build against the ACTUAL
# eligible object count — see _resolve_entity_bound — so an ordinary build
# never truncates arbitrarily; the hard ceiling below is purely a
# defensive circuit breaker against a truly pathological schema, never the
# normal operating limit.
_DEFAULT_MIN_MAX_ENTITIES = 200
_HARD_CEILING_MAX_ENTITIES = 5000
_DEFAULT_MIN_MAX_CANDIDATES_PER_ENTITY = 8
_HARD_CEILING_MAX_CANDIDATES_PER_ENTITY = 500

# Only used to key core.sqlservice.canonical_scoring.compute_canonical_scores'
# result identity field — the function itself is pure and never touches this
# identity for scoring math. compute_canonical_scores() (not
# load_canonical_scores()) is called directly below specifically so this
# constant identity is NEVER used as a cross-call cache key: a fixed cache
# key across different document sets would silently return a stale result.
_CANONICAL_IDENTITY = CanonicalScoringIdentity(
    build_id=0, embedding_provider="semantic_catalog", embedding_model="none", embedding_dimensions=0,
)

# ── Evidence-kind vocabulary (stable strings — machine-comparable) ────────

EVIDENCE_OBJECT_NAME_TOKEN = "OBJECT_NAME_TOKEN"
EVIDENCE_COLUMN_NAME_TOKEN = "COLUMN_NAME_TOKEN"
EVIDENCE_BUSINESS_NAME = "BUSINESS_NAME"
EVIDENCE_DESCRIPTION = "DESCRIPTION"
EVIDENCE_SYNONYM = "GENERATED_SYNONYM"
EVIDENCE_PRIMARY_KEY = "PRIMARY_KEY_STRUCTURE"
EVIDENCE_CONFIRMED_RELATIONSHIP = "CONFIRMED_RELATIONSHIP"
# Emitted wherever the underlying signal already passed through retrieval_
# documents' own data-type/dictionary-aware classification (status_columns/
# date_columns/measure_columns) — this module never re-derives a data type
# itself (none is available in the metadata contract), it only cites that
# an upstream, type-aware classifier already vetted the column.
EVIDENCE_STRUCTURAL_CATEGORY = "DATA_TYPE_CATEGORY"
EVIDENCE_RETRIEVAL_TIER = "RETRIEVAL_TIER"
EVIDENCE_CANONICAL_SCORE = "CANONICAL_SCORE"
EVIDENCE_FAMILY_GROUPING = "FAMILY_GROUPING"

IDENTIFIER_KIND_PRIMARY_KEY = "PRIMARY_KEY"
IDENTIFIER_KIND_FOREIGN_KEY = "FOREIGN_KEY"
IDENTIFIER_KIND_BUSINESS_IDENTIFIER = "BUSINESS_IDENTIFIER"
IDENTIFIER_KIND_TECHNICAL_IDENTIFIER = "TECHNICAL_IDENTIFIER"

# SQL Architecture Correction, Phase 6, Step 1A — entity-coverage
# accounting. Every eligible (Tier 1/2) object ends up in EXACTLY ONE of
# {an entity's candidate_objects, unclassified_objects with one of these
# reasons} — never silently dropped.
UNCLASSIFIED_REASON_NO_MEANINGFUL_NAME_TOKEN = "NO_MEANINGFUL_NAME_TOKEN"
UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY = "TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY"
UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_ENTITIES = "TRUNCATED_BY_MAX_ENTITIES"

# Two candidates within this confidence margin of each other are both
# treated as live possibilities (ambiguous=True) rather than one being
# silently preferred — applied identically to entity candidates and date-
# role candidates.
_AMBIGUITY_CONFIDENCE_MARGIN = 0.15


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Public data model (frozen, deeply immutable) ──────────────────────────


@dataclass(frozen=True)
class EvidenceItem:
    """One machine-readable justification for a semantic claim. `kind` is
    always one of the EVIDENCE_* constants above; `detail` is the exact
    token/value/name that grounds it (never a free-text explanation)."""

    kind: str
    detail: str


@dataclass(frozen=True)
class EntityCandidateObject:
    """One object proposed as a concrete representative of a BusinessEntity
    concept. Multiple candidates under the same entity are normal and are
    never collapsed to one — see BusinessEntity.ambiguous."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    confidence: float
    supporting_columns: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class BusinessEntity:
    """A generic business concept (e.g. "customer", "order") inferred from
    one or more objects' own names/documentation — never a hand-authored
    category. `confidence` is the best (max) candidate's own confidence;
    `ambiguous` is True when two or more candidates are both plausible
    representatives within _AMBIGUITY_CONFIDENCE_MARGIN of each other."""

    concept_id: str
    normalized_name: str
    aliases: tuple[str, ...]
    alias_total_count: int
    candidate_objects: tuple[EntityCandidateObject, ...]
    candidate_total_count: int
    confidence: float
    ambiguous: bool
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class MeasureConcept:
    """One column already structurally classified (upstream, by
    core.sqlservice.retrieval_documents) as a numeric, non-key business
    measure, and — when core.sqlservice.retrieval_documents' structured
    column_descriptors metadata is present — validated against its REAL SQL
    type (see confidence and this module's _type_gate). `aggregation_
    compatible` is still a generic default even when type-confirmed (this
    layer distinguishes numeric-vs-not, not precision/scale nuances — see
    this module's own docstring/known-boundaries note)."""

    object_fqn: str
    column_name: str
    normalized_measure: str  # "amount" | "quantity" | "score" | "balance" | "rate" | "count" | "duration" | "value"
    aggregation_compatible: tuple[str, ...]
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class DimensionConcept:
    """One column suitable for grouping/filtering. A single column MAY
    appear under more than one dimension_kind (e.g. a "location_type"
    column plausibly serves both) — dimensions are independent lenses, not
    a mutually-exclusive classification, so no signal is ever silently
    dropped in favor of another."""

    object_fqn: str
    column_name: str
    dimension_kind: str  # "status" | "type" | "category" | "program" | "location" | "flag"
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class DateRoleCandidate:
    role: str
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class DateRoleConcept:
    """One date/time column's role candidate(s). `ambiguous=True` means
    more than one role keyword category matched this column's name — ALL
    matched candidates are preserved (see `candidates`), never one silently
    chosen. A column matching no keyword category still gets one
    "unspecified" candidate at low confidence, rather than being dropped."""

    object_fqn: str
    column_name: str
    candidates: tuple[DateRoleCandidate, ...]
    ambiguous: bool


@dataclass(frozen=True)
class IdentifierConcept:
    """One column identified as a primary key, a confirmed foreign key, or
    a plausible business/technical identifier by name. A column ending in
    a generic "id"-shaped suffix with no PK/FK/business-keyword evidence is
    simply never classified as an identifier here at all."""

    object_fqn: str
    column_name: str
    identifier_kind: str  # IDENTIFIER_KIND_*
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class UnclassifiedObject:
    """An eligible (Tier 1/2) object that did NOT end up represented in any
    BusinessEntity's candidate_objects — SQL Architecture Correction, Phase
    6 Step 1A's entity-coverage fix. `reason` is one of the
    UNCLASSIFIED_REASON_* constants: the object either had no meaningful
    name token to seed a concept at all, or it was cut by a defensive
    max_entities/max_candidates_per_entity bound. Every eligible object
    appears in EXACTLY ONE of {some entity's candidate_objects, this list}
    — never silently dropped from the catalog entirely (see
    SemanticCatalog.coverage's represented_object_count +
    unclassified_object_count == eligible_object_count invariant)."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    reason: str


@dataclass(frozen=True)
class ObjectFamily:
    """A group of >= 2 objects core.sqlservice.canonical_scoring's own
    deterministic family detection judged comparable (shared significant
    name tokens, or overlapping confirmed-relationship neighborhoods).
    `canonical_object_fqn` is that algorithm's own top-ranked member.
    `narrow_or_similar` are other Tier 1/2 members; `archive_or_variant`
    are Tier 3/4 members (backup/archive/history/system objects the family
    detector still grouped in, typically via a shared name token)."""

    family_key: str
    canonical_object_fqn: str
    member_object_fqns: tuple[str, ...]
    narrow_or_similar: tuple[str, ...]
    archive_or_variant: tuple[str, ...]
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class ModelInferredObjectEvidence:
    """Bounded, ADVISORY model-proposed semantics for one REAL object
    already present in `documents` — SQL Architecture Correction, Phase 6
    Step 4B. Never a structural fact: nothing here ever changes
    `entities`/`measures`/`dimensions`/`date_roles`/`identifiers`/
    `object_families` above, which are computed identically whether or not
    `enrichment_input` was supplied at all (requirement 1: "structural
    facts always win"). `business_name_candidates`/`description_candidates`
    are recorded regardless of whether the object's OWN dictionary
    business_name/description is present (observability — mirrors
    core.sqlservice.semantic_enrichment_service.merge_object_semantics'
    own model_business_name_candidates convention); `dictionary_has_
    business_name`/`dictionary_has_description` is the ONLY signal any
    future consumer may ever treat as authority — a present dictionary
    value always wins and these candidates are never once folded into
    `entities` above regardless (requirement 2). Multiple disagreeing candidates are
    ALL retained (never one silently picked) with `ambiguous_*=True` when
    two are within `_MODEL_EVIDENCE_AMBIGUITY_MARGIN` of each other
    (requirement 6). `confidence` is the best (max) candidate confidence,
    always <= MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP (requirement 5).

    `business_domain_candidates`/`status_concept_label_candidates`/
    `supported_operation_candidates` (Phase 6, Step 6F, Part 4) are the
    SAME kind of dedup'd, advisory, provenance-carrying candidate text as
    `entity_concept_candidates`/`alias_candidates` -- never structural,
    never fed back into `entities`/`object_families` above, and empty for
    any enrichment_input entry whose type does not expose that attribute
    (e.g. a table-level OBJECT_CORE/COLUMN_DEFERRED entry, which has none
    of these three fields at all -- getattr's own default handles that
    silently, exactly like every other duck-typed field on this
    dataclass)."""

    object_fqn: str
    business_name_candidates: tuple[str, ...]
    description_candidates: tuple[str, ...]
    entity_concept_candidates: tuple[str, ...]
    alias_candidates: tuple[str, ...]
    object_role_candidates: tuple[tuple[str, float], ...]
    dictionary_has_business_name: bool
    dictionary_has_description: bool
    ambiguous_business_name: bool
    ambiguous_object_role: bool
    confidence: float
    evidence: tuple[EvidenceItem, ...]
    business_domain_candidates: tuple[str, ...] = ()
    status_concept_label_candidates: tuple[str, ...] = ()
    supported_operation_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelInferredColumnEvidence:
    """Bounded, ADVISORY model-proposed semantics for one REAL column of one
    REAL object already present in `documents`'s own column_names/
    column_descriptors metadata — Phase 6 Step 4B. `*_role_candidates` are
    (role, confidence) pairs, one entry per distinct role ANY matched
    enrichment_input row proposed for this column — never collapsed to a
    single "winning" role. An empty tuple means no model proposal existed
    for that role dimension at all, never a fabricated default. Never
    structural: this module never uses these candidates to reclassify a
    column as a measure/dimension/date/identifier in the sections above —
    that remains exclusively driven by structural metadata + upstream
    dictionary/type evidence, exactly as before Step 4B."""

    object_fqn: str
    column_name: str
    business_label_candidates: tuple[str, ...]
    semantic_role_candidates: tuple[tuple[str, float], ...]
    date_role_candidates: tuple[tuple[str, float], ...]
    measure_role_candidates: tuple[tuple[str, float], ...]
    dimension_role_candidates: tuple[tuple[str, float], ...]
    identifier_role_candidates: tuple[tuple[str, float], ...]
    confidence: float
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class SemanticCatalog:
    """The complete, immutable output of one build_semantic_catalog() call.
    `fingerprint` covers every semantic-content field below (see
    _compute_fingerprint) — two calls over the same document content (in
    any order) always produce the same fingerprint; any content change
    changes it. This includes `model_inferred_object_evidence`/
    `model_inferred_column_evidence`: usable enrichment content changing
    (Step 4B) changes `fingerprint` exactly like any other content change
    — the SAME mechanism core.sqlservice.schema_linker's own
    fingerprint-keyed index cache already relies on, so a new enrichment
    run can never accidentally reuse an old, un-enriched linker index
    without that module itself needing to change at all."""

    schema_fingerprint: str | None
    build_id: int | None
    entities: tuple[BusinessEntity, ...]
    measures: tuple[MeasureConcept, ...]
    dimensions: tuple[DimensionConcept, ...]
    date_roles: tuple[DateRoleConcept, ...]
    identifiers: tuple[IdentifierConcept, ...]
    object_families: tuple[ObjectFamily, ...]
    unclassified_objects: tuple[UnclassifiedObject, ...]
    model_inferred_object_evidence: tuple[ModelInferredObjectEvidence, ...]
    model_inferred_column_evidence: tuple[ModelInferredColumnEvidence, ...]
    warnings: tuple[str, ...]
    coverage: Mapping[str, object]
    fingerprint: str


# ── Local helpers: metadata field access (never document_text) ───────────


def _str_tuple(metadata: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = metadata.get(key) or ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


# ── Structured column-type contract (SQL Architecture Correction, Phase 6,
#    Step 1A) ─────────────────────────────────────────────────────────────
#
# core.sqlservice.retrieval_documents now attaches a "column_descriptors"
# metadata field — one {"column_name", "normalized_sql_type", "is_nullable",
# "is_primary_key", "is_foreign_key"} dict per column, using the SAME
# normalized type vocabulary core.connectors.schema.normalize_data_type
# already defines (TEXT | INTEGER | DECIMAL | DATETIME | BOOLEAN | BINARY |
# JSON | OTHER). This lets every concept extractor below VALIDATE a claim
# against a real SQL type instead of trusting column-name/upstream-flag
# classification alone — e.g. a dict-flagged "is_metric" column that is
# actually TEXT-typed (retrieval_documents._classify_column's is_metric
# branch does not itself check data type) no longer becomes a false measure.
#
# Contract for every type-gated concept below: "OTHER" and an absent/
# missing descriptor both mean UNKNOWN — never treated as a contradiction,
# never given the type-confirmed confidence bonus either (see
# _type_gate's own docstring) — "unknown types preserve low-confidence
# fallback behavior rather than fabricating certainty".
_UNKNOWN_TYPE_TOKENS = frozenset({"OTHER"})
_MEASURE_COMPATIBLE_TYPES = frozenset({"INTEGER", "DECIMAL"})
_DATE_COMPATIBLE_TYPES = frozenset({"DATETIME"})
_FLAG_COMPATIBLE_TYPES = frozenset({"BOOLEAN"})
_STATUS_COMPATIBLE_TYPES = frozenset({"TEXT", "INTEGER", "BOOLEAN"})
_TEXT_CATEGORY_COMPATIBLE_TYPES = frozenset({"TEXT"})


def _column_type_lookup(metadata: Mapping[str, object]) -> Mapping[str, str]:
    """column_name.lower() -> normalized_sql_type, from one document's own
    column_descriptors. Absent/malformed entries are simply skipped —
    never a crash — so a document from an older build that predates this
    field degrades to an empty lookup (every column reads as unknown)."""
    lookup: dict[str, str] = {}
    for descriptor in metadata.get("column_descriptors") or ():
        if not isinstance(descriptor, Mapping):
            continue
        name = descriptor.get("column_name")
        sql_type = descriptor.get("normalized_sql_type")
        if isinstance(name, str) and name and isinstance(sql_type, str) and sql_type:
            lookup[name.lower()] = sql_type
    return lookup


def _type_gate(
    column_name: str, type_lookup: Mapping[str, str], compatible_types: frozenset[str],
) -> tuple[bool, str | None]:
    """
    Returns (allowed, known_type).

    known_type is None whenever the type is unknown (no descriptor, or the
    descriptor's own type is "OTHER") — allowed is always True in that
    case: an unknown type never blocks a concept, it only withholds the
    type-confirmed confidence bonus (see each concept's own confidence
    formula), preserving pre-existing heuristic-only behavior.

    When known_type IS known and it is NOT in `compatible_types`, allowed
    is False — the caller excludes the concept entirely ("numeric measures
    REQUIRE compatible numeric types", etc. — a hard gate, not a
    confidence penalty), since real, contradicting type evidence is
    stronger than a name-pattern/dictionary-flag guess.
    """
    known_type = type_lookup.get(column_name.lower())
    if known_type is None or known_type in _UNKNOWN_TYPE_TOKENS:
        return True, None
    return (known_type in compatible_types), known_type


def _bare_column(descriptor: str) -> str | None:
    _object_fqn, sep, column = str(descriptor).rpartition(".")
    return column if sep else None


def _meaningful_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    """Tokens that look like real words — length >= 3, at least one letter,
    not purely numeric — the same generic bar
    core.sqlservice.canonical_scoring._score_name_quality already applies."""
    return tuple(t for t in tokens if len(t) >= 3 and any(c.isalpha() for c in t) and not t.isdigit())


def _bound_and_report(
    items: Sequence,
    max_items: int,
    *,
    label: str,
    warnings: list[str],
) -> tuple[tuple, int]:
    """Deterministic count-bound over an ALREADY-sorted sequence. Returns
    (kept, omitted_count) and appends a warning only when something was
    actually omitted (mirrors core.sqlservice.retrieval_documents'
    *_TOTAL/*_OMITTED convention)."""
    kept = tuple(items[:max_items])
    omitted = max(0, len(items) - max_items)
    if omitted:
        warnings.append(f"{label}: {omitted} item(s) truncated by max_items={max_items}")
    return kept, omitted


# ── Section 1: business entities ──────────────────────────────────────────


def _entity_head_noun(business_name: str | None, object_name: str) -> tuple[str | None, str]:
    """The single "head noun" token used as the entity's normalized_name —
    a generic English-compound-noun heuristic (the LAST significant token
    of a name is usually its head noun: "student_enrollments" -> tokens
    ["student", "enrollment"] -> "enrollment"), applied to business_name
    when present (stronger evidence), else the bare object name. Returns
    (normalized_name_or_None, source) where source is EVIDENCE_BUSINESS_NAME
    or EVIDENCE_OBJECT_NAME_TOKEN. None means no meaningful token was found
    at all (e.g. a purely numeric/cryptic object name) — the caller skips
    entity extraction for that object rather than fabricating a name."""
    if business_name:
        tokens = _meaningful_tokens(tokenize(business_name))
        if tokens:
            return tokens[-1], EVIDENCE_BUSINESS_NAME
    tokens = _meaningful_tokens(tokenize(object_name))
    if tokens:
        return tokens[-1], EVIDENCE_OBJECT_NAME_TOKEN
    return None, EVIDENCE_OBJECT_NAME_TOKEN


def _entity_candidate_confidence(
    *, source: str, tier: int, canonical_total_score: float, has_description: bool, has_synonyms: bool,
) -> float:
    """
    Bounded, documented formula (every term independently capped, sum <= 1.0
    before the tier adjustment):
      0.25 base           — the object exists and resolved to a name at all
      + 0.30 / 0.15       — normalized_name came from a curated business_name
                            (strong) vs. the bare object/table name (weaker)
      + 0.15              — the object also carries a human-written description
      + 0.10              — the object has generated-vocabulary synonym coverage
      + 0.20 * canonical   — core.sqlservice.canonical_scoring's own bounded
                            [0,1] per-build authority score, scaled down so it
                            can nudge but never dominate this formula
      - 0.05 / -0.15       — a small penalty for Tier 2 (supporting/mapping)
                            objects relative to Tier 1 (Tier 3/4 never reach
                            this function at all — see _ELIGIBLE_ENTITY_TIERS)
    """
    score = 0.25
    score += 0.30 if source == EVIDENCE_BUSINESS_NAME else 0.15
    score += 0.15 if has_description else 0.0
    score += 0.10 if has_synonyms else 0.0
    score += 0.20 * _clip01(canonical_total_score)
    score += 0.0 if tier == 1 else -0.05 if tier == 2 else -0.15
    return _clip01(score)


def _entity_supporting_columns(metadata: Mapping[str, object], max_columns: int) -> tuple[str, ...]:
    columns = sorted(
        set(_str_tuple(metadata, "primary_key_columns"))
        | set(_str_tuple(metadata, "measure_columns"))
        | set(_str_tuple(metadata, "status_columns"))
        | set(_str_tuple(metadata, "date_columns"))
    )
    return tuple(columns[:max_columns])


def _build_entities(
    documents: Sequence,
    canonical_total_score_by_key: Mapping[str, float],
    *,
    max_entities: int,
    max_aliases_per_entity: int,
    max_candidates_per_entity: int,
    max_supporting_columns_per_candidate: int,
    warnings: list[str],
) -> tuple[tuple[BusinessEntity, ...], tuple[UnclassifiedObject, ...]]:
    """
    Returns (entities, unclassified_objects). SQL Architecture Correction,
    Phase 6 Step 1A coverage guarantee: every eligible (Tier 1/2) document
    ends up EITHER inside some returned entity's candidate_objects OR in
    `unclassified_objects` with an explicit reason — never silently
    dropped by a bound. See UNCLASSIFIED_REASON_* for the three ways an
    eligible object can end up unclassified.
    """
    groups: dict[str, list[tuple]] = {}  # normalized_name.lower() -> [(doc, source, aliases_for_doc)]
    unclassified: list[UnclassifiedObject] = []

    for doc in documents:
        if doc.retrieval_tier not in _ELIGIBLE_ENTITY_TIERS:
            continue
        schema_name, _sep, object_name = doc.object_fqn.rpartition(".")
        business_name = doc.metadata.get("business_name")
        normalized_name, source = _entity_head_noun(business_name, object_name)
        if not normalized_name:
            unclassified.append(
                UnclassifiedObject(
                    object_fqn=doc.object_fqn, object_type=doc.object_type, retrieval_tier=doc.retrieval_tier,
                    reason=UNCLASSIFIED_REASON_NO_MEANINGFUL_NAME_TOKEN,
                )
            )
            continue

        name_tokens = set(_meaningful_tokens(tokenize(business_name))) if business_name else set()
        name_tokens |= set(_meaningful_tokens(tokenize(object_name)))
        name_tokens.discard(normalized_name)
        synonyms = _str_tuple(doc.metadata, "synonyms")

        groups.setdefault(normalized_name.lower(), []).append(
            (doc, source, normalized_name, sorted(name_tokens), synonyms)
        )

    entities: list[BusinessEntity] = []
    for key in sorted(groups):
        members = groups[key]
        normalized_name = members[0][2]  # canonical spelling: first in sorted-key-group, deterministic

        candidates: list[EntityCandidateObject] = []
        # Phase 6, Step 6J, Part 1 fix — curated `synonyms` (deliberate,
        # per-object business vocabulary) are tracked SEPARATELY from
        # auto-derived name-token fragments (split from the object's own
        # business_name/object_name). A real audit proved that pooling
        # BOTH kinds into one set, then truncating alphabetically, let a
        # multi-object entity's own bulk of derived fragments (from OTHER
        # member objects) crowd a DIFFERENT member's few curated synonyms
        # out of the bounded alias list purely by spelling — e.g.
        # "enrollment"/"fee"/"fee paid" (all three genuinely present in
        # one object's own `synonyms` metadata) alphabetically sorting to
        # positions 66/69/70 in a 172-token pooled set with a 60-item
        # bound, even though nothing about their INFORMATION VALUE
        # justified dropping them ahead of some other object's raw
        # name-token split. See `curated_alias_pool`/`derived_alias_pool`
        # below.
        curated_alias_pool: set[str] = set()
        derived_alias_pool: set[str] = set()
        synonym_present = False

        for doc, source, _norm, name_tokens, synonyms in members:
            canonical_score = canonical_total_score_by_key.get(doc.object_fqn.lower(), 0.0)
            has_description = bool(doc.metadata.get("description"))
            has_synonyms = bool(synonyms)
            synonym_present = synonym_present or has_synonyms
            confidence = _entity_candidate_confidence(
                source=source, tier=doc.retrieval_tier, canonical_total_score=canonical_score,
                has_description=has_description, has_synonyms=has_synonyms,
            )
            evidence = [EvidenceItem(kind=source, detail=normalized_name)]
            if has_description:
                evidence.append(EvidenceItem(kind=EVIDENCE_DESCRIPTION, detail=doc.object_fqn))
            if has_synonyms:
                evidence.append(EvidenceItem(kind=EVIDENCE_SYNONYM, detail=synonyms[0]))
            evidence.append(EvidenceItem(kind=EVIDENCE_RETRIEVAL_TIER, detail=str(doc.retrieval_tier)))
            evidence.append(EvidenceItem(kind=EVIDENCE_CANONICAL_SCORE, detail=f"{canonical_score:.4f}"))

            candidates.append(
                EntityCandidateObject(
                    object_fqn=doc.object_fqn,
                    object_type=doc.object_type,
                    retrieval_tier=doc.retrieval_tier,
                    confidence=confidence,
                    supporting_columns=_entity_supporting_columns(doc.metadata, max_supporting_columns_per_candidate),
                    evidence=tuple(evidence),
                )
            )
            derived_alias_pool.update(name_tokens)
            curated_alias_pool.update(s for s in synonyms if s.lower() != normalized_name.lower())

        candidates.sort(key=lambda c: (-c.confidence, c.object_fqn))
        candidate_total_count = len(candidates)
        bounded_candidates = tuple(candidates[:max_candidates_per_entity])
        overflow_candidates = candidates[max_candidates_per_entity:]
        if overflow_candidates:
            warnings.append(
                f"entity {normalized_name!r} candidate objects: {len(overflow_candidates)} "
                f"item(s) truncated by max_candidates_per_entity={max_candidates_per_entity}"
            )
            unclassified.extend(
                UnclassifiedObject(
                    object_fqn=c.object_fqn, object_type=c.object_type, retrieval_tier=c.retrieval_tier,
                    reason=UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY,
                )
                for c in overflow_candidates
            )

        # Curated synonyms sort and truncate FIRST — deliberate, curated
        # evidence always outranks an auto-derived name-token fragment,
        # never the reverse (mirrors this codebase's own "structural/
        # curated evidence outranks derived/weak evidence" convention —
        # e.g. core.sqlservice.schema_linker.MODEL_CANDIDATE_CONFIDENCE_CAP
        # — applied here to alias PROVENANCE rather than match confidence).
        # Both tiers remain independently alphabetically sorted (fully
        # deterministic); a token present in both pools counts once, as a
        # curated synonym.
        curated_sorted = tuple(sorted(a for a in curated_alias_pool if a))
        derived_sorted = tuple(sorted(a for a in derived_alias_pool if a and a not in curated_alias_pool))
        ordered_aliases = curated_sorted + derived_sorted
        alias_total_count = len(ordered_aliases)
        bounded_aliases, _omitted_aliases = _bound_and_report(
            ordered_aliases, max_aliases_per_entity,
            label=f"entity {normalized_name!r} aliases", warnings=warnings,
        )

        best_confidence = bounded_candidates[0].confidence if bounded_candidates else 0.0
        ambiguous = len(bounded_candidates) > 1 and sum(
            1 for c in bounded_candidates if best_confidence - c.confidence <= _AMBIGUITY_CONFIDENCE_MARGIN
        ) > 1

        concept_evidence = [EvidenceItem(kind=EVIDENCE_OBJECT_NAME_TOKEN, detail=normalized_name)]
        if synonym_present:
            concept_evidence.append(EvidenceItem(kind=EVIDENCE_SYNONYM, detail="generated_vocabulary_present"))

        entities.append(
            BusinessEntity(
                concept_id=f"entity:{key}",
                normalized_name=normalized_name,
                aliases=bounded_aliases,
                alias_total_count=alias_total_count,
                candidate_objects=bounded_candidates,
                candidate_total_count=candidate_total_count,
                confidence=best_confidence,
                ambiguous=ambiguous,
                evidence=tuple(concept_evidence),
            )
        )

    entities.sort(key=lambda e: (-e.confidence, e.normalized_name))
    bounded_entities = entities[:max_entities]
    overflow_entities = entities[max_entities:]
    if overflow_entities:
        warnings.append(f"entities: {len(overflow_entities)} item(s) truncated by max_entities={max_entities}")
        for entity in overflow_entities:
            unclassified.extend(
                UnclassifiedObject(
                    object_fqn=c.object_fqn, object_type=c.object_type, retrieval_tier=c.retrieval_tier,
                    reason=UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_ENTITIES,
                )
                for c in entity.candidate_objects
            )

    unclassified.sort(key=lambda u: (u.object_fqn, u.reason))
    return tuple(bounded_entities), tuple(unclassified)


# ── Section 2: measures ────────────────────────────────────────────────────
#
# Ordered, first-match-wins keyword categories — order matters (checked top
# to bottom) so a more specific term (e.g. "balance") is never shadowed by a
# broader catch-all (e.g. "amount"/"value") purely by dict ordering.
# Deliberately generic: no domain-specific column name appears here, only
# common English business-numeric vocabulary.
_MEASURE_KEYWORD_CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("balance", frozenset({"balance"})),
    ("score", frozenset({"score"})),
    ("rate", frozenset({"rate", "percent", "pct"})),
    ("quantity", frozenset({"qty", "quantity"})),
    ("count", frozenset({"count", "sum"})),
    ("duration", frozenset({"duration", "day", "month", "year", "hour", "minute", "age"})),
    ("amount", frozenset({
        "amount", "amt", "total", "price", "cost", "revenue", "fee", "charge", "payment", "value", "val",
    })),
)

_MEASURE_AGGREGATION_DEFAULT = ("SUM", "AVG", "MIN", "MAX", "COUNT")
_MEASURE_AGGREGATION_COUNT = ("COUNT", "SUM", "MIN", "MAX")


def _classify_measure(column_name: str) -> tuple[str, bool]:
    """Returns (normalized_measure, decisive) — decisive=False means no
    keyword category matched and the generic "value" fallback was used
    (still a legitimate measure: it already passed retrieval_documents'
    own type/PK/identity-aware upstream classification — see
    core.sqlservice.retrieval_documents._classify_column)."""
    tokens = set(tokenize(column_name))
    for category, keywords in _MEASURE_KEYWORD_CATEGORIES:
        if tokens & keywords:
            return category, True
    return "value", False


def _measure_confidence(*, decisive: bool, type_confirmed: bool, has_documentation: bool, tier: int) -> float:
    """
    0.45 base   — already structurally classified upstream as measure_columns
                  (never a primary key/identity column — see
                  core.sqlservice.retrieval_documents._classify_column)
    + 0.10      — a decisive keyword category matched (not the generic
                  "value" catch-all)
    + 0.15      — the column's REAL SQL type is known and confirmed numeric
                  (INTEGER/DECIMAL) — this module's own Phase 6 Step 1A
                  addition; 0.0 (never fabricated) when the type is unknown
    + 0.15      — the owning object has a business_name or description
    + 0.15/0.05 — Tier 1 vs Tier 2 (Tier 3/4 objects can still carry
                  measure_columns in metadata; this module does not exclude
                  them from measures, only discounts their confidence)
    Bounded to [0, 1]; a fully-corroborated Tier 1 measure reaches exactly
    1.0, an unknown-type Tier 1 measure tops out at 0.85 (the pre-Step-1A
    fallback shape, never inflated by a type bonus it didn't earn).
    """
    score = 0.45
    score += 0.10 if decisive else 0.0
    score += 0.15 if type_confirmed else 0.0
    score += 0.15 if has_documentation else 0.0
    score += 0.15 if tier == 1 else 0.05 if tier == 2 else 0.0
    return _clip01(score)


def _build_measures(documents: Sequence, *, max_measures: int, warnings: list[str]) -> tuple[MeasureConcept, ...]:
    measures: list[MeasureConcept] = []
    for doc in documents:
        has_documentation = bool(doc.metadata.get("business_name")) or bool(doc.metadata.get("description"))
        type_lookup = _column_type_lookup(doc.metadata)
        for column in _str_tuple(doc.metadata, "measure_columns"):
            allowed, known_type = _type_gate(column, type_lookup, _MEASURE_COMPATIBLE_TYPES)
            if not allowed:
                # A REAL, known, non-numeric type contradicts the upstream
                # "measure" claim (e.g. a dict-flagged is_metric column that
                # is actually TEXT-typed) — "numeric measures require
                # compatible numeric types" is a hard gate, never a mere
                # confidence penalty.
                continue
            normalized_measure, decisive = _classify_measure(column)
            confidence = _measure_confidence(
                decisive=decisive, type_confirmed=known_type is not None,
                has_documentation=has_documentation, tier=doc.retrieval_tier,
            )
            aggregation = _MEASURE_AGGREGATION_COUNT if normalized_measure == "count" else _MEASURE_AGGREGATION_DEFAULT
            evidence = [
                EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail="measure_columns"),
                EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column),
                EvidenceItem(kind=EVIDENCE_RETRIEVAL_TIER, detail=str(doc.retrieval_tier)),
            ]
            if known_type is not None:
                evidence.append(EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail=f"normalized_sql_type={known_type}"))
            measures.append(
                MeasureConcept(
                    object_fqn=doc.object_fqn, column_name=column, normalized_measure=normalized_measure,
                    aggregation_compatible=aggregation, confidence=confidence, evidence=tuple(evidence),
                )
            )

    measures.sort(key=lambda m: (-m.confidence, m.object_fqn, m.column_name))
    bounded, _omitted = _bound_and_report(measures, max_measures, label="measures", warnings=warnings)
    return bounded


# ── Section 3: dimensions and categories ──────────────────────────────────

_DIMENSION_TOKEN_CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    ("type", frozenset({"type", "kind"})),
    ("category", frozenset({"category"})),
    ("program", frozenset({"program", "plan", "package", "tier"})),
    ("location", frozenset({
        "location", "city", "state", "region", "country", "address", "zip", "postal", "site", "branch",
    })),
)

_FLAG_LEAD_TOKENS = frozenset({"is", "has"})
_FLAG_TAIL_TOKEN = "flag"

# Raw (non-stopword-filtered) split — deliberately NOT
# core.sqlservice.lexical_index.tokenize(), because "is"/"has" are exactly
# the signal a flag-name check needs, and tokenize() correctly treats both
# as English stop words for prose/business-name matching (it would silently
# strip them here). Same generic snake_case/CamelCase/digit-boundary split
# core.sqlservice.canonical_scoring._local_tokenize already uses, small
# enough to duplicate rather than import (this module has no other reason
# to depend on canonical_scoring's private tokenizer).
_RAW_SPLIT_PATTERN = re.compile(
    r"[_\-\s./\\,;:!?()\[\]{}'\"]+"
    r"|(?<=[a-z0-9])(?=[A-Z])"
    r"|(?<=[A-Za-z])(?=[0-9])"
    r"|(?<=[0-9])(?=[A-Za-z])",
)


def _raw_tokens(text: object) -> tuple[str, ...]:
    if not text:
        return ()
    parts = [p for p in _RAW_SPLIT_PATTERN.split(str(text)) if p]
    return tuple(p.lower() for p in parts)


def _looks_like_flag_name(column_name: str) -> bool:
    """CamelCase-safe generic boolean-flag naming check (mirrors
    core.sqlservice.retrieval_documents._STATUS_NAME_PATTERN's intent, but
    token-based so "IsActive" and "is_active" match identically): the
    FIRST raw token is "is"/"has", or the LAST raw token is "flag"."""
    tokens = _raw_tokens(column_name)
    if not tokens:
        return False
    return tokens[0] in _FLAG_LEAD_TOKENS or tokens[-1] == _FLAG_TAIL_TOKEN


def _dimension_type_compatible_types(kind: str) -> frozenset[str]:
    """Which normalized SQL types are consistent with a dimension_kind —
    "boolean dimensions use real boolean/bit-compatible types; text
    categories require compatible text types" (Phase 6 Step 1A). "status"
    is intentionally broader than "flag": a status can legitimately be a
    text code, a small integer code, or a real boolean, whereas a "flag"
    (is_/has_/_flag-named) column specifically implies boolean/bit."""
    if kind == "flag":
        return _FLAG_COMPATIBLE_TYPES
    if kind == "status":
        return _STATUS_COMPATIBLE_TYPES
    return _TEXT_CATEGORY_COMPATIBLE_TYPES  # type | category | program | location


def _dimension_confidence(*, corroborated_by_upstream: bool, type_confirmed: bool, tier: int) -> float:
    """
    0.60 base for a column already in retrieval_documents' own
    type/dictionary-aware status_columns (corroborated_by_upstream=True);
    0.30 base for a purely name-only match against a generic category
    keyword. + 0.15 when the column's REAL SQL type is known and confirmed
    compatible with this dimension_kind (Phase 6 Step 1A addition; 0.0,
    never fabricated, when the type is unknown). +/- a small Tier 1/2
    adjustment, always bounded to [0, 1].
    """
    base = 0.60 if corroborated_by_upstream else 0.30
    base += 0.15 if type_confirmed else 0.0
    base += 0.05 if tier == 1 else -0.05 if tier == 2 else -0.15
    return _clip01(base)


def _build_dimensions(documents: Sequence, *, max_dimensions: int, warnings: list[str]) -> tuple[DimensionConcept, ...]:
    dimensions: list[DimensionConcept] = []
    for doc in documents:
        status_columns = set(_str_tuple(doc.metadata, "status_columns"))
        date_columns = set(_str_tuple(doc.metadata, "date_columns"))
        measure_columns = set(_str_tuple(doc.metadata, "measure_columns"))
        type_lookup = _column_type_lookup(doc.metadata)

        for column in sorted(status_columns):
            allowed, known_type = _type_gate(column, type_lookup, _dimension_type_compatible_types("status"))
            if not allowed:
                continue
            confidence = _dimension_confidence(corroborated_by_upstream=True, type_confirmed=known_type is not None, tier=doc.retrieval_tier)
            evidence = [
                EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail="status_columns"),
                EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column),
            ]
            if known_type is not None:
                evidence.append(EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail=f"normalized_sql_type={known_type}"))
            dimensions.append(
                DimensionConcept(
                    object_fqn=doc.object_fqn, column_name=column, dimension_kind="status", confidence=confidence,
                    evidence=tuple(evidence),
                )
            )

        for column in sorted(_str_tuple(doc.metadata, "column_names")):
            if column in status_columns or column in date_columns or column in measure_columns:
                continue
            tokens = set(tokenize(column))
            for kind, keywords in _DIMENSION_TOKEN_CATEGORIES:
                if not (tokens & keywords):
                    continue
                allowed, known_type = _type_gate(column, type_lookup, _dimension_type_compatible_types(kind))
                if not allowed:
                    continue
                confidence = _dimension_confidence(corroborated_by_upstream=False, type_confirmed=known_type is not None, tier=doc.retrieval_tier)
                evidence = [EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column)]
                if known_type is not None:
                    evidence.append(EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail=f"normalized_sql_type={known_type}"))
                dimensions.append(
                    DimensionConcept(
                        object_fqn=doc.object_fqn, column_name=column, dimension_kind=kind, confidence=confidence,
                        evidence=tuple(evidence),
                    )
                )
            if _looks_like_flag_name(column):
                allowed, known_type = _type_gate(column, type_lookup, _dimension_type_compatible_types("flag"))
                if allowed:
                    confidence = _dimension_confidence(corroborated_by_upstream=False, type_confirmed=known_type is not None, tier=doc.retrieval_tier)
                    evidence = [EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column)]
                    if known_type is not None:
                        evidence.append(EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail=f"normalized_sql_type={known_type}"))
                    dimensions.append(
                        DimensionConcept(
                            object_fqn=doc.object_fqn, column_name=column, dimension_kind="flag", confidence=confidence,
                            evidence=tuple(evidence),
                        )
                    )

    dimensions.sort(key=lambda d: (-d.confidence, d.object_fqn, d.column_name, d.dimension_kind))
    bounded, _omitted = _bound_and_report(dimensions, max_dimensions, label="dimensions", warnings=warnings)
    return bounded


# ── Section 4: date roles ─────────────────────────────────────────────────

_DATE_ROLE_KEYWORDS: tuple[tuple[str, frozenset[str]], ...] = (
    ("created", frozenset({"created", "create", "creation"})),
    ("updated", frozenset({"updated", "update", "modified", "modify", "changed", "change"})),
    ("started", frozenset({"start", "started", "begin", "began"})),
    ("completed", frozenset({
        "completed", "complete", "end", "ended", "finish", "finished", "closed", "close", "terminated", "termination",
    })),
    ("enrollment", frozenset({
        "enroll", "enrolled", "enrollment", "signup", "sign", "registration", "registered", "join", "joined",
    })),
    ("transaction", frozenset({
        "transaction", "payment", "paid", "pay", "billing", "bill", "invoice", "invoiced",
    })),
    ("effective", frozenset({"effective", "expiry", "expire", "expired", "expiration", "valid", "validity"})),
)

_DATE_ROLE_UNSPECIFIED = "unspecified"
_DATE_ROLE_UNSPECIFIED_CONFIDENCE = 0.30


def _date_role_confidence(*, matched_category_count: int, type_confirmed: bool, tier: int) -> float:
    """0.35 base (already a structurally classified date_columns entry) + a
    decisive bonus (0.25 for exactly one matched keyword category, 0.10
    when more than one matched — each individual match is inherently less
    certain when the name is ambiguous between roles) + 0.15 when the
    column's REAL SQL type is known and confirmed DATETIME-compatible
    (Phase 6 Step 1A addition; 0.0, never fabricated, when unknown) + a
    small Tier 1/2 bonus, bounded to [0, 1]."""
    base = 0.35 + (0.25 if matched_category_count == 1 else 0.10)
    base += 0.15 if type_confirmed else 0.0
    base += 0.1 if tier == 1 else 0.03 if tier == 2 else 0.0
    return _clip01(base)


def _build_date_roles(documents: Sequence, *, max_date_roles: int, warnings: list[str]) -> tuple[DateRoleConcept, ...]:
    date_roles: list[DateRoleConcept] = []
    for doc in documents:
        type_lookup = _column_type_lookup(doc.metadata)
        for column in sorted(_str_tuple(doc.metadata, "date_columns")):
            allowed, known_type = _type_gate(column, type_lookup, _DATE_COMPATIBLE_TYPES)
            if not allowed:
                # A REAL, known, non-date type contradicts the "date_columns"
                # claim (e.g. a dict-flagged is_date column that is actually
                # TEXT-typed) -- "date roles require date/time-compatible
                # types" is a hard gate: a real date must never be confused
                # with a merely date-LIKE text column.
                continue

            tokens = set(tokenize(column))
            matched = [role for role, keywords in _DATE_ROLE_KEYWORDS if tokens & keywords]
            type_evidence = (
                (EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail=f"normalized_sql_type={known_type}"),)
                if known_type is not None else ()
            )

            if matched:
                confidence = _date_role_confidence(
                    matched_category_count=len(matched), type_confirmed=known_type is not None, tier=doc.retrieval_tier,
                )
                candidates = tuple(
                    DateRoleCandidate(
                        role=role, confidence=confidence,
                        evidence=(
                            EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail="date_columns"),
                            EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column),
                            *type_evidence,
                        ),
                    )
                    for role in sorted(matched)
                )
            else:
                candidates = (
                    DateRoleCandidate(
                        role=_DATE_ROLE_UNSPECIFIED, confidence=_DATE_ROLE_UNSPECIFIED_CONFIDENCE,
                        evidence=(EvidenceItem(kind=EVIDENCE_STRUCTURAL_CATEGORY, detail="date_columns"), *type_evidence),
                    ),
                )

            date_roles.append(
                DateRoleConcept(
                    object_fqn=doc.object_fqn, column_name=column,
                    candidates=candidates, ambiguous=len(candidates) > 1,
                )
            )

    date_roles.sort(key=lambda d: (-d.candidates[0].confidence, d.object_fqn, d.column_name))
    bounded, _omitted = _bound_and_report(date_roles, max_date_roles, label="date_roles", warnings=warnings)
    return bounded


# ── Section 5: identifiers and keys ───────────────────────────────────────

_BUSINESS_IDENTIFIER_TOKENS = frozenset({
    "email", "phone", "ssn", "ein", "username", "login", "license", "code", "number", "num",
})
_TECHNICAL_IDENTIFIER_TAIL_TOKENS = frozenset({"id", "guid", "uuid", "key"})


def _build_identifiers(documents: Sequence, *, max_identifiers: int, warnings: list[str]) -> tuple[IdentifierConcept, ...]:
    identifiers: list[IdentifierConcept] = []
    for doc in documents:
        primary_keys = set(_str_tuple(doc.metadata, "primary_key_columns"))
        for column in sorted(primary_keys):
            identifiers.append(
                IdentifierConcept(
                    object_fqn=doc.object_fqn, column_name=column, identifier_kind=IDENTIFIER_KIND_PRIMARY_KEY,
                    confidence=1.0, evidence=(EvidenceItem(kind=EVIDENCE_PRIMARY_KEY, detail=column),),
                )
            )

        fk_columns: set[str] = set()
        for edge in doc.metadata.get("confirmed_relationship_edges") or ():
            if edge.get("direction") != "outbound":
                continue
            column = edge.get("column")
            if not isinstance(column, str) or not column:
                continue
            fk_columns.add(column)
            identifiers.append(
                IdentifierConcept(
                    object_fqn=doc.object_fqn, column_name=column, identifier_kind=IDENTIFIER_KIND_FOREIGN_KEY,
                    confidence=0.9,
                    evidence=(
                        EvidenceItem(kind=EVIDENCE_CONFIRMED_RELATIONSHIP, detail=str(edge.get("other") or "")),
                    ),
                )
            )

        classified = primary_keys | fk_columns
        for column in sorted(_str_tuple(doc.metadata, "column_names")):
            if column in classified:
                continue
            tokens = set(tokenize(column))
            if tokens & _BUSINESS_IDENTIFIER_TOKENS:
                identifiers.append(
                    IdentifierConcept(
                        object_fqn=doc.object_fqn, column_name=column,
                        identifier_kind=IDENTIFIER_KIND_BUSINESS_IDENTIFIER, confidence=0.5,
                        evidence=(EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column),),
                    )
                )
                continue
            token_list = tokenize(column)
            # A generic trailing "id"/"guid"/"uuid"/"key" token alone is
            # ONLY ever evidence of a technical/surrogate identifier — never
            # promoted to a business identifier by this rule (see this
            # module's own docstring/IdentifierConcept note).
            if token_list and token_list[-1] in _TECHNICAL_IDENTIFIER_TAIL_TOKENS:
                identifiers.append(
                    IdentifierConcept(
                        object_fqn=doc.object_fqn, column_name=column,
                        identifier_kind=IDENTIFIER_KIND_TECHNICAL_IDENTIFIER, confidence=0.35,
                        evidence=(EvidenceItem(kind=EVIDENCE_COLUMN_NAME_TOKEN, detail=column),),
                    )
                )

    identifiers.sort(key=lambda i: (-i.confidence, i.object_fqn, i.column_name, i.identifier_kind))
    bounded, _omitted = _bound_and_report(identifiers, max_identifiers, label="identifiers", warnings=warnings)
    return bounded


# ── Section 6: object families (reuses canonical_scoring verbatim) ───────


def _build_object_families(
    documents: Sequence,
    canonical_scores_by_key: Mapping[str, object],
    *,
    max_object_families: int,
    warnings: list[str],
) -> tuple[ObjectFamily, ...]:
    tier_by_key = {d.object_fqn.lower(): d.retrieval_tier for d in documents}

    members_by_family: dict[str, list] = {}
    for key, score in canonical_scores_by_key.items():
        members_by_family.setdefault(score.family_key, []).append(score)

    families: list[ObjectFamily] = []
    for family_key in sorted(members_by_family):
        members = members_by_family[family_key]
        if len(members) < 2:
            continue  # a "family" of one is just an ordinary, uncompared object
        members.sort(key=lambda s: (s.family_rank, s.object_fqn))
        canonical_member = next(s for s in members if s.family_rank == 1)

        narrow_or_similar = tuple(sorted(
            s.object_fqn for s in members
            if s.object_fqn != canonical_member.object_fqn and tier_by_key.get(s.object_fqn.lower(), 1) in (1, 2)
        ))
        archive_or_variant = tuple(sorted(
            s.object_fqn for s in members
            if s.object_fqn != canonical_member.object_fqn and tier_by_key.get(s.object_fqn.lower(), 1) in (3, 4)
        ))

        families.append(
            ObjectFamily(
                family_key=family_key,
                canonical_object_fqn=canonical_member.object_fqn,
                member_object_fqns=tuple(s.object_fqn for s in members),
                narrow_or_similar=narrow_or_similar,
                archive_or_variant=archive_or_variant,
                confidence=canonical_member.total_score,
                evidence=(
                    EvidenceItem(kind=EVIDENCE_FAMILY_GROUPING, detail=f"{len(members)} member(s)"),
                    EvidenceItem(kind=EVIDENCE_CANONICAL_SCORE, detail=f"{canonical_member.total_score:.4f}"),
                ),
            )
        )

    families.sort(key=lambda f: (-f.confidence, f.canonical_object_fqn))
    bounded, _omitted = _bound_and_report(families, max_object_families, label="object_families", warnings=warnings)
    return bounded


# ── Section 7: model-inferred advisory evidence (Phase 6, Step 4B) ────────
#
# Reads ONLY `documents` (for real object/column existence — never a
# database, never a provider) and `enrichment_input` (see this module's own
# Step 4B comment above `MODEL_INFERRED_PROVENANCE_KIND`). Produces
# evidence ADDITIONAL to, and completely separate from, every structural
# section above — this is the ONLY section of build_semantic_catalog() that
# ever reads enrichment_input, and its output never feeds back into
# entities/measures/dimensions/date_roles/identifiers/object_families.


def _clip_model_confidence(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(_clip01(numeric), MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP)


def _has_model_inferred_provenance(provenance: object) -> bool:
    for item in provenance or ():
        if getattr(item, "kind", None) == MODEL_INFERRED_PROVENANCE_KIND:
            return True
    return False


def _dedup_candidates(values) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        key = v.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v.strip())
    return tuple(sorted(out))


def _rank_candidates(pairs: Sequence[tuple[str, float]]) -> tuple[tuple[str, float], ...]:
    """One entry per DISTINCT label, confidence = the max seen for it —
    never collapsed further than that; sorted by confidence descending,
    label ascending as a deterministic tie-break. An empty `pairs` yields
    an empty tuple, never a fabricated placeholder."""
    best: dict[str, float] = {}
    for label, confidence in pairs:
        if not isinstance(label, str) or not label:
            continue
        best[label] = max(best.get(label, 0.0), confidence)
    return tuple(sorted(best.items(), key=lambda kv: (-kv[1], kv[0])))


def _ambiguous_candidates(ranked: Sequence[tuple[str, float]]) -> bool:
    return len(ranked) > 1 and (ranked[0][1] - ranked[1][1]) <= _MODEL_EVIDENCE_AMBIGUITY_MARGIN


def _real_column_names_lower(metadata: Mapping[str, object]) -> frozenset[str]:
    descriptor_names = {
        str(d.get("column_name")).lower()
        for d in (metadata.get("column_descriptors") or ())
        if isinstance(d, Mapping) and isinstance(d.get("column_name"), str) and d.get("column_name")
    }
    return frozenset(descriptor_names | {c.lower() for c in _str_tuple(metadata, "column_names")})


def _build_model_inferred_evidence(
    documents: Sequence,
    enrichment_input: Sequence | None,
    *,
    max_model_inferred_object_evidence: int,
    max_model_inferred_column_evidence: int,
    warnings: list[str],
) -> tuple[tuple[ModelInferredObjectEvidence, ...], tuple[ModelInferredColumnEvidence, ...], dict[str, int]]:
    """
    Returns (object_evidence, column_evidence, rejected_by_reason) —
    `rejected_by_reason` maps each REJECTED_REASON_* constant to how many
    enrichment_input entries/columns were rejected for that exact reason
    (surfaced via `coverage['model_inferred_rejected_by_reason']` and
    `coverage['model_inferred_rejected_count']`) — never silently dropped
    without a trace, mirroring this module's own unclassified_objects
    discipline for structural coverage.

    Every `enrichment_input` entry — and each of its `.columns` — is
    checked against REAL structure already present in `documents` (a real
    object_fqn; for a column, a real column_name of that SAME object)
    before ever being folded in (REJECTED_REASON_UNKNOWN_OBJECT/
    REJECTED_REASON_UNKNOWN_COLUMN — "model inference may not create
    objects or columns", requirement 4), and must carry at least one
    MODEL_INFERRED_SEMANTIC-kind provenance item (REJECTED_REASON_MISSING_
    PROVENANCE — "preserve provenance", requirement 5).

    Dictionary precedence (requirement 2): candidates are recorded
    regardless of dictionary presence (see `dictionary_has_business_name`/
    `dictionary_has_description` on ModelInferredObjectEvidence) — "never
    even offered a candidate slot" is NOT how this is enforced; instead a
    present dictionary value simply never has any code path that could
    replace it, and these candidates never feed back into `entities` above.
    """
    if not enrichment_input:
        return (), (), {}

    doc_by_fqn_lower = {d.object_fqn.lower(): d for d in documents}
    rejected_by_reason: dict[str, int] = {}

    def _reject(reason: str) -> None:
        rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1

    grouped_object_entries: dict[str, list] = {}
    for entry in enrichment_input:
        entry_fqn = getattr(entry, "object_fqn", None)
        doc = doc_by_fqn_lower.get(entry_fqn.lower()) if isinstance(entry_fqn, str) else None
        if doc is None:
            _reject(REJECTED_REASON_UNKNOWN_OBJECT)
            continue
        if not _has_model_inferred_provenance(getattr(entry, "provenance", ())):
            _reject(REJECTED_REASON_MISSING_PROVENANCE)
            continue
        grouped_object_entries.setdefault(entry_fqn.lower(), []).append(entry)

    object_evidence: list[ModelInferredObjectEvidence] = []
    column_groups: dict[tuple[str, str], list[tuple[object, str]]] = {}

    for key in sorted(grouped_object_entries):
        entries = grouped_object_entries[key]
        doc = doc_by_fqn_lower[key]
        canonical_fqn = doc.object_fqn
        dictionary_has_business_name = bool(doc.metadata.get("business_name"))
        dictionary_has_description = bool(doc.metadata.get("description"))
        real_columns_lower = _real_column_names_lower(doc.metadata)

        name_pairs: list[tuple[str, float]] = []
        description_values: list[str] = []
        role_pairs: list[tuple[str, float]] = []
        concept_values: list[str] = []
        alias_values: list[str] = []
        domain_values: list[str] = []
        status_label_values: list[str] = []
        operation_values: list[str] = []
        best_confidence = 0.0

        for entry in entries:
            confidence = _clip_model_confidence(getattr(entry, "confidence", 0.0))
            best_confidence = max(best_confidence, confidence)

            # Candidates are recorded regardless of dictionary presence (for
            # observability/audit — mirrors core.sqlservice.
            # semantic_enrichment_service.merge_object_semantics' own
            # model_business_name_candidates convention exactly). "Dictionary
            # wins" is enforced by `dictionary_has_business_name`/
            # `dictionary_has_description` below being the ONLY signal any
            # future consumer may treat as authoritative — NOT by hiding the
            # candidate — and, structurally, by these candidates never once
            # feeding back into `entities` above regardless.
            business_name = getattr(entry, "business_name", None)
            if isinstance(business_name, str) and business_name.strip():
                name_pairs.append((business_name.strip(), confidence))

            description = getattr(entry, "description", None)
            if isinstance(description, str) and description.strip():
                description_values.append(description.strip())

            object_role = getattr(entry, "object_role", None)
            if isinstance(object_role, str) and object_role:
                role_pairs.append((object_role, confidence))

            concept_values.extend(c for c in (getattr(entry, "entity_concepts", ()) or ()) if isinstance(c, str))
            alias_values.extend(a for a in (getattr(entry, "aliases", ()) or ()) if isinstance(a, str))

            # Phase 6, Step 6F, Part 4 -- VIEW_ENRICHED-only fields (absent,
            # via getattr's own default, from a table-level OBJECT_CORE/
            # COLUMN_DEFERRED entry, which has no business_domain/
            # status_concepts/supported_operations attribute at all).
            domain = getattr(entry, "business_domain", None)
            if isinstance(domain, str) and domain.strip():
                domain_values.append(domain.strip())
            for status_concept in getattr(entry, "status_concepts", ()) or ():
                label = getattr(status_concept, "label", None)
                if isinstance(label, str) and label.strip():
                    status_label_values.append(label.strip())
            operation_values.extend(
                o for o in (getattr(entry, "supported_operations", ()) or ()) if isinstance(o, str)
            )

            for col in getattr(entry, "columns", ()) or ():
                col_name = getattr(col, "column_name", None)
                if not isinstance(col_name, str) or col_name.lower() not in real_columns_lower:
                    _reject(REJECTED_REASON_UNKNOWN_COLUMN)
                    continue
                if not _has_model_inferred_provenance(getattr(col, "provenance", ())):
                    _reject(REJECTED_REASON_MISSING_PROVENANCE)
                    continue
                column_groups.setdefault((key, col_name.lower()), []).append((col, col_name))

        name_ranked = _rank_candidates(name_pairs)
        role_ranked = _rank_candidates(role_pairs)

        object_evidence.append(
            ModelInferredObjectEvidence(
                object_fqn=canonical_fqn,
                business_name_candidates=tuple(label for label, _c in name_ranked),
                description_candidates=_dedup_candidates(description_values),
                entity_concept_candidates=_dedup_candidates(concept_values),
                alias_candidates=_dedup_candidates(alias_values),
                object_role_candidates=role_ranked,
                dictionary_has_business_name=dictionary_has_business_name,
                dictionary_has_description=dictionary_has_description,
                ambiguous_business_name=_ambiguous_candidates(name_ranked),
                ambiguous_object_role=_ambiguous_candidates(role_ranked),
                confidence=best_confidence,
                evidence=(EvidenceItem(kind=MODEL_INFERRED_PROVENANCE_KIND, detail=canonical_fqn),),
                business_domain_candidates=_dedup_candidates(domain_values),
                status_concept_label_candidates=_dedup_candidates(status_label_values),
                supported_operation_candidates=_dedup_candidates(operation_values),
            )
        )

    column_evidence: list[ModelInferredColumnEvidence] = []
    for obj_key, col_key in sorted(column_groups):
        col_entries = column_groups[(obj_key, col_key)]
        canonical_fqn = doc_by_fqn_lower[obj_key].object_fqn
        canonical_col_name = col_entries[0][1]

        label_values: list[str] = []
        semantic_pairs: list[tuple[str, float]] = []
        date_pairs: list[tuple[str, float]] = []
        measure_pairs: list[tuple[str, float]] = []
        dimension_pairs: list[tuple[str, float]] = []
        identifier_pairs: list[tuple[str, float]] = []
        best_confidence = 0.0

        for col, _name in col_entries:
            confidence = _clip_model_confidence(getattr(col, "confidence", 0.0))
            best_confidence = max(best_confidence, confidence)

            label = getattr(col, "business_label", None)
            if isinstance(label, str) and label.strip():
                label_values.append(label.strip())

            semantic_role = getattr(col, "semantic_role", None)
            if isinstance(semantic_role, str) and semantic_role:
                semantic_pairs.append((semantic_role, confidence))

            for attr_name, bucket in (
                ("date_role", date_pairs), ("measure_role", measure_pairs),
                ("dimension_role", dimension_pairs), ("identifier_role", identifier_pairs),
            ):
                role_value = getattr(col, attr_name, None)
                if isinstance(role_value, str) and role_value:
                    bucket.append((role_value, confidence))

        column_evidence.append(
            ModelInferredColumnEvidence(
                object_fqn=canonical_fqn,
                column_name=canonical_col_name,
                business_label_candidates=_dedup_candidates(label_values),
                semantic_role_candidates=_rank_candidates(semantic_pairs),
                date_role_candidates=_rank_candidates(date_pairs),
                measure_role_candidates=_rank_candidates(measure_pairs),
                dimension_role_candidates=_rank_candidates(dimension_pairs),
                identifier_role_candidates=_rank_candidates(identifier_pairs),
                confidence=best_confidence,
                evidence=(
                    EvidenceItem(kind=MODEL_INFERRED_PROVENANCE_KIND, detail=f"{canonical_fqn}.{canonical_col_name}"),
                ),
            )
        )

    object_evidence.sort(key=lambda e: e.object_fqn.lower())
    column_evidence.sort(key=lambda e: (e.object_fqn.lower(), e.column_name.lower()))

    bounded_objects, _omitted_o = _bound_and_report(
        object_evidence, max_model_inferred_object_evidence,
        label="model_inferred_object_evidence", warnings=warnings,
    )
    bounded_columns, _omitted_c = _bound_and_report(
        column_evidence, max_model_inferred_column_evidence,
        label="model_inferred_column_evidence", warnings=warnings,
    )
    return bounded_objects, bounded_columns, rejected_by_reason


# ── Fingerprint (deterministic, order-independent) ────────────────────────


def _to_plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"semantic catalog fingerprint: unsupported value type {type(value).__name__!r}")


def _compute_fingerprint(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(_to_plain(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Public entry point ─────────────────────────────────────────────────────


def _resolve_entity_bound(
    value: int | None, *, param_name: str, eligible_object_count: int, default_min: int, hard_ceiling: int,
) -> int:
    """
    None -> schema-scaled default: large enough that an ordinary build
    never truncates arbitrarily (at least `eligible_object_count`, so even
    the pathological case of every eligible object landing in ONE entity
    concept/group still fits), floored at `default_min` (keeps small-schema
    behavior sane) and capped at `hard_ceiling` (the actual defensive
    limit — a genuinely enormous schema still gets a bounded catalog, with
    the resulting truncation reported exactly like an explicit caller
    value would be).

    An explicit caller value is validated the same way every other max_*
    parameter is (positive integer) and additionally may never exceed
    `hard_ceiling` — "configurable... with a hard production ceiling" is a
    real ceiling, not a suggestion a caller can override.
    """
    if value is None:
        return min(hard_ceiling, max(default_min, eligible_object_count))
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{param_name} must be a positive integer or None, got {value!r}")
    if value > hard_ceiling:
        raise ValueError(f"{param_name} ({value}) exceeds the hard production ceiling of {hard_ceiling}")
    return value


def build_semantic_catalog(
    documents: Sequence,
    *,
    max_entities: int | None = None,
    # SQL Architecture Correction, Phase 6, Step 3E, Part 3 — raised from an
    # original 12: aliases are bounded ALPHABETICALLY (see _build_entities'
    # own `aliases_sorted = tuple(sorted(...))`), so a small cap on a table
    # with dozens of descriptive columns systematically discarded whichever
    # aliases happened to sort later in the alphabet regardless of business
    # value — a real audit found a genuinely useful synonym (a borrowed
    # "program"-type column fragment) silently cut purely because ~20 other
    # aliases starting with earlier letters filled the cap first, making a
    # legitimate synonym-based alternative candidate permanently
    # unreachable. 60 is still a bound (never unbounded growth for a
    # pathological wide table) but comfortably covers real-world entities'
    # full alias vocabulary — a generic capacity increase applied uniformly
    # to every entity, never tuned to any specific table/word.
    max_aliases_per_entity: int = 60,
    max_candidates_per_entity: int | None = None,
    max_supporting_columns_per_candidate: int = 20,
    max_measures: int = 4000,
    max_dimensions: int = 4000,
    max_date_roles: int = 4000,
    max_identifiers: int = 8000,
    max_object_families: int = 500,
    max_warnings: int = 200,
    max_model_inferred_object_evidence: int = 4000,
    max_model_inferred_column_evidence: int = 4000,
    schema_fingerprint: str | None = None,
    build_id: int | None = None,
    enrichment_input: Sequence | None = None,
) -> SemanticCatalog:
    """
    Build a deterministic SemanticCatalog from an already-loaded document
    sequence (any object exposing object_fqn/object_type/retrieval_tier/
    metadata — RetrievalDocument, IndexedSchemaDocument, and
    StoredRetrievalDocument all satisfy this, matching the same duck-typed
    contract core.sqlservice.vector_index/lexical_index/canonical_scoring
    already accept).

    `enrichment_input` (SQL Architecture Correction, Phase 6, Step 4B) is an
    OPTIONAL, immutable sequence of already-validated, already-persisted
    model semantics — duck-typed the same way `documents` itself is (see
    core.sqlservice.semantic_enrichment_store.StoredSemanticEnrichment,
    which exposes exactly the attributes this module reads; NOT imported
    here, to avoid a circular dependency — see this module's own Step 4B
    comment above MODEL_INFERRED_PROVENANCE_KIND). Every entry is folded
    into `model_inferred_object_evidence`/`model_inferred_column_evidence`
    ONLY — bounded, advisory evidence that never changes `entities`/
    `measures`/`dimensions`/`date_roles`/`identifiers`/`object_families`
    above in any way: those sections are computed IDENTICALLY whether or
    not `enrichment_input` is supplied at all ("structural facts always
    win", Step 4B requirement 1). `enrichment_input=None` (the default)
    produces a catalog byte-for-byte identical to never having this
    parameter at all.

    Pure and in-memory: no database, network, or provider call of any kind;
    `schema_fingerprint`/`build_id` are optional caller-supplied identity
    fields carried through verbatim (never computed or verified here — see
    data/schema_fingerprint_service.py for that, which this module never
    duplicates).

    `max_entities`/`max_candidates_per_entity` default to None — a
    schema-scaled default (see _resolve_entity_bound) that never
    arbitrarily truncates an ordinary build, bounded by a hard production
    ceiling a caller cannot exceed even explicitly (Phase 6 Step 1A —
    "separate safety bounds from arbitrary loss of semantic coverage").
    Every OTHER `max_*` argument is a hard, defensive bound — a category
    that would otherwise exceed it is deterministically truncated (highest
    confidence kept, canonical-order tie-break) and the truncation is
    reported both as a warning and via `coverage`'s own `*_omitted_count`
    entries; nothing is ever silently dropped without a trace. In
    particular, every eligible (Tier 1/2) object is guaranteed to appear in
    EXACTLY ONE of {an entity's candidate_objects, `unclassified_objects`}
    — see `coverage`'s eligible_object_count/represented_object_count/
    unclassified_object_count/truncated_object_count/entity_coverage_
    percentage fields.

    Raises:
        ValueError: `documents` contains two entries with the same
            object_fqn (case-insensitively), any `max_*` argument is not a
            positive integer (or None, for max_entities/max_candidates_
            per_entity), or an explicit max_entities/max_candidates_per_
            entity exceeds its hard production ceiling.
    """
    bound_params = {
        "max_aliases_per_entity": max_aliases_per_entity,
        "max_supporting_columns_per_candidate": max_supporting_columns_per_candidate,
        "max_measures": max_measures, "max_dimensions": max_dimensions,
        "max_date_roles": max_date_roles, "max_identifiers": max_identifiers,
        "max_object_families": max_object_families, "max_warnings": max_warnings,
        "max_model_inferred_object_evidence": max_model_inferred_object_evidence,
        "max_model_inferred_column_evidence": max_model_inferred_column_evidence,
    }
    for name, value in bound_params.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")

    sorted_documents = sorted(documents, key=lambda d: d.object_fqn.lower())
    fqns_lower = [d.object_fqn.lower() for d in sorted_documents]
    if len(fqns_lower) != len(set(fqns_lower)):
        raise ValueError("documents contains duplicate object_fqn values (case-insensitive)")

    eligible_object_count = sum(1 for d in sorted_documents if d.retrieval_tier in _ELIGIBLE_ENTITY_TIERS)
    resolved_max_entities = _resolve_entity_bound(
        max_entities, param_name="max_entities", eligible_object_count=eligible_object_count,
        default_min=_DEFAULT_MIN_MAX_ENTITIES, hard_ceiling=_HARD_CEILING_MAX_ENTITIES,
    )
    resolved_max_candidates_per_entity = _resolve_entity_bound(
        max_candidates_per_entity, param_name="max_candidates_per_entity", eligible_object_count=eligible_object_count,
        default_min=_DEFAULT_MIN_MAX_CANDIDATES_PER_ENTITY, hard_ceiling=_HARD_CEILING_MAX_CANDIDATES_PER_ENTITY,
    )

    warnings: list[str] = []

    canonical_result = compute_canonical_scores(sorted_documents, _CANONICAL_IDENTITY)
    canonical_total_score_by_key = {
        key: score.total_score for key, score in canonical_result.scores_by_fqn_lower.items()
    }

    entities, unclassified_objects = _build_entities(
        sorted_documents, canonical_total_score_by_key,
        max_entities=resolved_max_entities, max_aliases_per_entity=max_aliases_per_entity,
        max_candidates_per_entity=resolved_max_candidates_per_entity,
        max_supporting_columns_per_candidate=max_supporting_columns_per_candidate,
        warnings=warnings,
    )
    measures = _build_measures(sorted_documents, max_measures=max_measures, warnings=warnings)
    dimensions = _build_dimensions(sorted_documents, max_dimensions=max_dimensions, warnings=warnings)
    date_roles = _build_date_roles(sorted_documents, max_date_roles=max_date_roles, warnings=warnings)
    identifiers = _build_identifiers(sorted_documents, max_identifiers=max_identifiers, warnings=warnings)
    object_families = _build_object_families(
        sorted_documents, canonical_result.scores_by_fqn_lower,
        max_object_families=max_object_families, warnings=warnings,
    )
    model_inferred_object_evidence, model_inferred_column_evidence, model_inferred_rejected_by_reason = (
        _build_model_inferred_evidence(
            sorted_documents, enrichment_input,
            max_model_inferred_object_evidence=max_model_inferred_object_evidence,
            max_model_inferred_column_evidence=max_model_inferred_column_evidence,
            warnings=warnings,
        )
    )

    tier_counts = {1: 0, 2: 0, 3: 0, 4: 0}
    objects_with_business_name = 0
    objects_with_description = 0
    for doc in sorted_documents:
        tier_counts[doc.retrieval_tier] = tier_counts.get(doc.retrieval_tier, 0) + 1
        if doc.metadata.get("business_name"):
            objects_with_business_name += 1
        if doc.metadata.get("description"):
            objects_with_description += 1

    warnings_kept, warnings_omitted = _bound_and_report(warnings, max_warnings, label="warnings", warnings=[])
    # (the recursive-looking call above intentionally passes a throwaway
    # warnings sink — a "warnings about too many warnings" message would
    # itself need bounding, so it is reported only via coverage below.)

    represented_object_count = sum(len(e.candidate_objects) for e in entities)
    unclassified_object_count = len(unclassified_objects)
    truncated_object_count = sum(
        1 for u in unclassified_objects
        if u.reason in (
            UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_CANDIDATES_PER_ENTITY,
            UNCLASSIFIED_REASON_TRUNCATED_BY_MAX_ENTITIES,
        )
    )
    entity_coverage_percentage = (
        100.0 if eligible_object_count == 0 else 100.0 * represented_object_count / eligible_object_count
    )

    coverage = MappingProxyType({
        "total_objects": len(sorted_documents),
        "tier_1_objects": tier_counts[1],
        "tier_2_objects": tier_counts[2],
        "tier_3_objects": tier_counts[3],
        "tier_4_objects": tier_counts[4],
        "excluded_tier_3_4_object_count": tier_counts[3] + tier_counts[4],
        "objects_with_business_name": objects_with_business_name,
        "objects_with_description": objects_with_description,
        "entity_concept_count": len(entities),
        "entity_candidate_object_count": represented_object_count,
        "ambiguous_entity_count": sum(1 for e in entities if e.ambiguous),
        "measure_count": len(measures),
        "dimension_count": len(dimensions),
        "date_role_count": len(date_roles),
        "ambiguous_date_role_count": sum(1 for d in date_roles if d.ambiguous),
        "identifier_count": len(identifiers),
        "object_family_count": len(object_families),
        "warnings_omitted_count": warnings_omitted,
        # Phase 6, Step 1A — entity-coverage accounting (separate from the
        # bounds above, which protect memory/size; these fields report
        # whether coverage was actually lost, and by how much).
        "eligible_object_count": eligible_object_count,
        "represented_object_count": represented_object_count,
        "unclassified_object_count": unclassified_object_count,
        "truncated_object_count": truncated_object_count,
        "entity_coverage_percentage": entity_coverage_percentage,
        "resolved_max_entities": resolved_max_entities,
        "resolved_max_candidates_per_entity": resolved_max_candidates_per_entity,
        # Phase 6, Step 4B — advisory model-inference coverage. Zero/zero/0
        # for every call that never supplies enrichment_input (the default),
        # so this key is purely additive to any existing coverage consumer.
        "model_inferred_object_evidence_count": len(model_inferred_object_evidence),
        "model_inferred_column_evidence_count": len(model_inferred_column_evidence),
        "model_inferred_rejected_count": sum(model_inferred_rejected_by_reason.values()),
        "model_inferred_rejected_by_reason": dict(model_inferred_rejected_by_reason),
    })

    fingerprint_payload = {
        "fingerprint_version": SEMANTIC_CATALOG_FINGERPRINT_VERSION,
        "schema_fingerprint": schema_fingerprint,
        "build_id": build_id,
        "entities": entities,
        "measures": measures,
        "dimensions": dimensions,
        "date_roles": date_roles,
        "identifiers": identifiers,
        "object_families": object_families,
        "unclassified_objects": unclassified_objects,
        "model_inferred_object_evidence": model_inferred_object_evidence,
        "model_inferred_column_evidence": model_inferred_column_evidence,
        "warnings": warnings_kept,
        "coverage": dict(coverage),
    }
    fingerprint = _compute_fingerprint(fingerprint_payload)

    return SemanticCatalog(
        schema_fingerprint=schema_fingerprint,
        build_id=build_id,
        entities=entities,
        measures=measures,
        dimensions=dimensions,
        date_roles=date_roles,
        identifiers=identifiers,
        object_families=object_families,
        unclassified_objects=unclassified_objects,
        model_inferred_object_evidence=model_inferred_object_evidence,
        model_inferred_column_evidence=model_inferred_column_evidence,
        warnings=warnings_kept,
        coverage=coverage,
        fingerprint=fingerprint,
    )
