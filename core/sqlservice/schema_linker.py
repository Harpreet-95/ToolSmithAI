"""
Deterministic question-to-schema linker — SQL Architecture Correction,
Phase 6, Steps 2, 2A, and 2B.

Translates a natural-language business question into structured
QuestionRequirements (operation, matched entity/measure/dimension/date-role/
identifier REQUIREMENTS — each a single normalized business concept with
multiple ranked schema candidates underneath, comparison/trend/grouping
intent, unmatched terms, unresolved filter values) and a ranked, concept-
coverage-aware set of AnchorCandidate objects across the COMPLETE eligible
(Tier 1/2) schema — never a fixed top-K pre-filter. This is groundwork for a
future join-subgraph planner; this module does NOT plan joins, generate
SQL, execute anything, or call a database/embedding/LLM provider of any
kind.

Reuse (this module does not duplicate any of the following):
  - core.sqlservice.semantic_catalog.SemanticCatalog — the ONLY source of
    entity/measure/dimension/date-role/identifier concepts. This module
    never re-derives a concept from raw metadata; it only INDEXES and
    MATCHES what the catalog already computed.
  - core.sqlservice.semantic_catalog's measure/dimension/date-role keyword
    category tables (_MEASURE_KEYWORD_CATEGORIES, _DIMENSION_TOKEN_
    CATEGORIES, _DATE_ROLE_KEYWORDS) — imported directly (this module sits
    strictly ABOVE semantic_catalog in the dependency graph, so there is no
    cycle risk the sibling-module "duplicate the tokenizer" convention
    exists to avoid).
  - core.sqlservice.lexical_index.tokenize — all question/column
    tokenization (snake_case/CamelCase splitting, singularization, English
    stop-word removal).
  - core.sqlservice.canonical_scoring.compute_canonical_scores — the SAME
    per-build canonical-authority score AnchorCandidate.canonical_score
    reports, never a second/conflicting scoring algorithm. Object-family
    membership (core.sqlservice.semantic_catalog.SemanticCatalog.
    object_families) is reused verbatim for ambiguity/family-distinctness
    checks — never a second family algorithm.
  - core.sqlservice.vector_index.VectorSearchResult / core.sqlservice.
    lexical_index.LexicalSearchResult shapes for `semantic_rankings`/
    `lexical_rankings` — this module never embeds a question or computes a
    BM25 score itself; both are supplied by the caller (any object exposing
    `.object_fqn`/`.score` satisfies the contract).
  - core.sqlservice.sql_example_retrieval.RetrievedSqlExample's shape for
    `retrieved_examples` (`.proposed_object_fqns`/`.similarity`) — this
    module never retrieves examples itself.

Scope: pure, in-memory, no I/O of any kind — no database connection, no
embedding/LLM/provider call, no SQL Server, no OpenAI. The question string
itself is used only transiently inside one link_question_to_schema() call —
never stored, logged, or included in any cached structure or in this
result's own `fingerprint` (see that field's own docstring).

No single customer's specific table/column name or production constant
appears anywhere in this file — every keyword table below is generic
English business-question vocabulary, the same level of genericness
core.sqlservice.retrieval_documents/canonical_scoring/lexical_index/
semantic_catalog already hold themselves to.

── Phase 6, Step 2B: normalized requirements (this revision) ─────────────

Step 2A removed unconditional canonical-only anchor admission, but a
widely-repeated column (e.g. a "ClassID"-shaped foreign key present on
dozens of tables) still produced one INDEPENDENT concept per (object,
column) pair, and concept-coverage selection believed it had to cover
every one of them — reproducing an anchor explosion through a different
door. Step 2B fixes the ROOT CAUSE: matching now aggregates every match
into a MatchedRequirement — one normalized business concept (keyed by
concept kind + normalized label, deliberately NEVER by object FQN or
column) — carrying an ordered list of RequirementCandidate entries (one
per (object, column), each with its own confidence/evidence). Concept-
coverage selection now needs only ONE strong candidate to consider a
requirement covered; the existing Step 2A ambiguity-margin/canonical-
family machinery (unchanged in its own logic) decides which OTHER
candidates for that same requirement are still genuinely plausible
alternatives worth keeping as extra anchors, and drops the rest. A bounded
IDF-like informativeness factor (core.sqlservice.semantic_catalog's own
"do not tune against golden cases" convention applies here identically)
down-weights a candidate whose matched token is common across many
distinct objects in THIS catalog — computed purely from structural
document frequency, never a hardcoded token list — while an entity's own
exact-name/synonym phrase match is never touched by it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from core.config import (
    SQL_LINKING_COMMON_TOKEN_INFORMATIVENESS_FLOOR,
    SQL_LINKING_MAX_AMBIGUITY_ALTERNATIVES_PER_REQUIREMENT,
    SQL_LINKING_MAX_AMBIGUITY_RECORDS,
    SQL_LINKING_MAX_ANCHOR_CANDIDATES,
    SQL_LINKING_MAX_CACHED_INDEXES,
    SQL_LINKING_MAX_CANDIDATES_PER_REQUIREMENT,
    SQL_LINKING_MAX_EVIDENCE_ITEMS_PER_ANCHOR,
    SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK,
    SQL_LINKING_MAX_MATCHED_CONCEPTS,
    SQL_LINKING_MAX_NON_CONCEPT_ANCHOR_FILL,
    SQL_LINKING_MAX_QUESTION_CHARACTERS,
    SQL_LINKING_MAX_UNMATCHED_TERMS,
    SQL_LINKING_MAX_WARNINGS,
    SQL_LINKING_MIN_AMBIGUITY_CONFIDENCE,
    SQL_LINKING_MIN_ANCHOR_CONFIDENCE,
    SQL_LINKING_MIN_EXAMPLE_SIMILARITY,
    SQL_LINKING_MIN_SEMANTIC_SCORE,
)
from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, compute_canonical_scores
from core.sqlservice.lexical_index import tokenize
from core.sqlservice.semantic_catalog import (
    _DATE_ROLE_KEYWORDS,
    _DIMENSION_TOKEN_CATEGORIES,
    _MEASURE_KEYWORD_CATEGORIES,
    BusinessEntity,
    DateRoleConcept,
    DimensionConcept,
    EvidenceItem,
    IdentifierConcept,
    MeasureConcept,
    ModelInferredColumnEvidence,
    ModelInferredObjectEvidence,
    SemanticCatalog,
)

__all__ = [
    "STATUS_OK",
    "STATUS_EMPTY_SCHEMA",
    "STATUS_NO_CONFIDENT_ANCHOR",
    "STATUS_INDEX_BUILD_FAILED",
    "ADMISSION_REASON_CONCEPT_MATCH",
    "ADMISSION_REASON_LEXICAL_MATCH",
    "ADMISSION_REASON_SEMANTIC_MATCH",
    "ADMISSION_REASON_EXAMPLE_MATCH",
    "ADMISSION_REASON_MODEL_MATCH",
    "OPERATION_ROW_COUNT",
    "OPERATION_FILTERED_COUNT",
    "OPERATION_LIST_DETAIL",
    "OPERATION_GROUPED_AGGREGATION",
    "OPERATION_SUM",
    "OPERATION_AVERAGE",
    "OPERATION_MINIMUM",
    "OPERATION_MAXIMUM",
    "OPERATION_RANKING_TOP_N",
    "OPERATION_COMPARISON",
    "OPERATION_TREND",
    "OPERATION_EXISTENCE",
    "OPERATION_DISTINCT_COUNT",
    "OPERATION_UNKNOWN",
    "CONCEPT_KIND_ENTITY",
    "CONCEPT_KIND_MEASURE",
    "CONCEPT_KIND_DIMENSION",
    "CONCEPT_KIND_DATE_ROLE",
    "CONCEPT_KIND_IDENTIFIER",
    "MATCH_KIND_EXACT_BUSINESS_NAME",
    "MATCH_KIND_SYNONYM",
    "MATCH_KIND_COLUMN_TOKEN",
    "MATCH_KIND_CATEGORY_KEYWORD",
    "MATCH_KIND_MODEL_SEMANTIC",
    "PROVENANCE_MODEL_INFERRED_SEMANTIC",
    "MODEL_CANDIDATE_CONFIDENCE_CAP",
    "AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES",
    "AMBIGUITY_MULTIPLE_DATE_ROLES",
    "AMBIGUITY_MULTIPLE_MEASURES",
    "AMBIGUITY_UNCLEAR_AGGREGATION_TARGET",
    "AMBIGUITY_CONFLICTING_CANONICAL_CANDIDATES",
    "STRENGTH_CORE",
    "STRENGTH_CONSTRAINT",
    "STRENGTH_SUPPORTING",
    "STRENGTH_UNRESOLVED",
    "ANCHOR_WEIGHT_CATALOG",
    "ANCHOR_WEIGHT_SEMANTIC",
    "ANCHOR_WEIGHT_LEXICAL",
    "ANCHOR_WEIGHT_CANONICAL",
    "ANCHOR_WEIGHT_EXAMPLE",
    "ANCHOR_WEIGHT_MODEL_SEMANTIC",
    "RANK_SATURATION",
    "ANCHOR_AMBIGUITY_MARGIN",
    "RequirementCandidate",
    "MatchedRequirement",
    "QuestionRequirements",
    "AnchorCandidate",
    "AmbiguityRecord",
    "SchemaLinkingLimits",
    "DEFAULT_SCHEMA_LINKING_LIMITS",
    "SchemaLinkingResult",
    "link_question_to_schema",
    "clear_schema_linker_cache",
    "schema_linker_cache_size",
    "prepare_linker_index",
]

# ── Status vocabulary ──────────────────────────────────────────────────────

STATUS_OK = "OK"
STATUS_EMPTY_SCHEMA = "EMPTY_SCHEMA"  # no eligible (Tier 1/2) object exists in this catalog/document set
# Phase 6, Step 2A — a typed, non-error result for "nothing in the schema
# is actually relevant to this question" (zero relevance-qualified objects,
# or every qualified object fell below the final anchor-confidence floor)
# — returned INSTEAD of padding the anchor list with unrelated, purely-
# canonical-authority objects. See SchemaLinkingResult.no_confident_anchor_
# reason for which of the two conditions applied.
STATUS_NO_CONFIDENT_ANCHOR = "NO_CONFIDENT_ANCHOR"
# Phase 6, Step 3E — a typed, non-partial failure for "the schema-derived
# index could not be built safely" (e.g. a malformed document raised while
# indexing). Returned INSTEAD of letting an exception propagate mid-way
# through a partially-built index, and instead of silently continuing with
# a truncated/incomplete result — see link_question_to_schema's own index-
# construction try/except.
STATUS_INDEX_BUILD_FAILED = "INDEX_BUILD_FAILED"

# ── Admission-evidence vocabulary (Phase 6, Step 2A) ───────────────────────
#
# Every AnchorCandidate.admission_reasons entry is one of these — the
# EXACT signal(s) that qualified the object for consideration at all.
# Canonical authority is deliberately NOT in this list: it ranks among
# already-admitted candidates (see ANCHOR_WEIGHT_CANONICAL) but can never
# by itself be a reason in this tuple.
ADMISSION_REASON_CONCEPT_MATCH = "CONCEPT_MATCH"
ADMISSION_REASON_LEXICAL_MATCH = "LEXICAL_MATCH"
ADMISSION_REASON_SEMANTIC_MATCH = "SEMANTIC_MATCH"
ADMISSION_REASON_EXAMPLE_MATCH = "EXAMPLE_MATCH"
# SQL Architecture Correction, Phase 6, Step 4B1 — an object with ZERO
# structural/dictionary catalog support may still be admitted purely
# because a validated model-inferred alias/business-label/column-role
# maps it to a requirement the question ALREADY expresses (see
# _score_all_eligible_objects' own model_semantic_score, computed
# separately from catalog_score) — "model evidence may make an object
# available as a terminal candidate" (Step 4B1, Part 6). Never conflated
# with ADMISSION_REASON_CONCEPT_MATCH, which remains structural/
# dictionary-only, so admission observability can always tell the two apart.
ADMISSION_REASON_MODEL_MATCH = "MODEL_MATCH"

# ── Operation vocabulary (generic analytical intents) ─────────────────────

OPERATION_ROW_COUNT = "ROW_COUNT"
OPERATION_FILTERED_COUNT = "FILTERED_COUNT"
OPERATION_LIST_DETAIL = "LIST_DETAIL"
OPERATION_GROUPED_AGGREGATION = "GROUPED_AGGREGATION"
OPERATION_SUM = "SUM"
OPERATION_AVERAGE = "AVERAGE"
OPERATION_MINIMUM = "MINIMUM"
OPERATION_MAXIMUM = "MAXIMUM"
OPERATION_RANKING_TOP_N = "RANKING_TOP_N"
OPERATION_COMPARISON = "COMPARISON"
OPERATION_TREND = "TREND"
OPERATION_EXISTENCE = "EXISTENCE"
OPERATION_DISTINCT_COUNT = "DISTINCT_COUNT"
OPERATION_UNKNOWN = "UNKNOWN"

# ── Concept/match/ambiguity vocabulary ─────────────────────────────────────

CONCEPT_KIND_ENTITY = "ENTITY"
CONCEPT_KIND_MEASURE = "MEASURE"
CONCEPT_KIND_DIMENSION = "DIMENSION"
CONCEPT_KIND_DATE_ROLE = "DATE_ROLE"
CONCEPT_KIND_IDENTIFIER = "IDENTIFIER"

MATCH_KIND_EXACT_BUSINESS_NAME = "EXACT_BUSINESS_NAME"
MATCH_KIND_SYNONYM = "SYNONYM"
MATCH_KIND_COLUMN_TOKEN = "COLUMN_TOKEN"
MATCH_KIND_CATEGORY_KEYWORD = "CATEGORY_KEYWORD"
# SQL Architecture Correction, Phase 6, Step 4B1 — the ONE match kind every
# candidate sourced from core.sqlservice.semantic_catalog.SemanticCatalog.
# model_inferred_object_evidence/model_inferred_column_evidence ever
# carries. Used as the sole, structural (never heuristic) signal this
# module's own tiering (see _build_requirement) relies on to guarantee
# "structural/canonical spelling always wins" and "dictionary-confirmed
# candidates always outrank otherwise-equivalent model-only candidates"
# (Part 2) — a candidate of this kind is NEVER sorted ahead of one of any
# other kind, regardless of raw confidence.
MATCH_KIND_MODEL_SEMANTIC = "MODEL_SEMANTIC"

# Intentionally duplicates the STRING VALUE of core.sqlservice.
# semantic_enrichment_service.PROVENANCE_MODEL_INFERRED_SEMANTIC / core.
# sqlservice.semantic_catalog.MODEL_INFERRED_PROVENANCE_KIND (never
# imported — this module sits above semantic_catalog and must never depend
# on semantic_enrichment_service at all, avoiding both a layering
# violation and a circular import). Two independent modules agreeing on
# one stable, machine-comparable evidence-kind string, the same level of
# coupling every other EVIDENCE_*/MATCH_KIND_* constant in this codebase
# already has.
PROVENANCE_MODEL_INFERRED_SEMANTIC = "MODEL_INFERRED_SEMANTIC"

# Defense in depth (Part 5: "preserve confidence cap"): re-clipped here
# independently of whatever confidence core.sqlservice.semantic_catalog's
# own model_inferred_*_evidence already claims to have capped — this
# module never trusts an upstream claim of "already capped" without
# re-enforcing it itself. Numerically identical to core.sqlservice.
# semantic_catalog.MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP and core.
# sqlservice.semantic_enrichment_service.MODEL_INFERRED_CONFIDENCE_CAP by
# deliberate convention, never by import.
MODEL_CANDIDATE_CONFIDENCE_CAP = 0.50

AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES = "MULTIPLE_ENTITY_CANDIDATES"
AMBIGUITY_MULTIPLE_DATE_ROLES = "MULTIPLE_DATE_ROLES"
AMBIGUITY_MULTIPLE_MEASURES = "MULTIPLE_MEASURES"
AMBIGUITY_UNCLEAR_AGGREGATION_TARGET = "UNCLEAR_AGGREGATION_TARGET"
AMBIGUITY_CONFLICTING_CANONICAL_CANDIDATES = "CONFLICTING_CANONICAL_CANDIDATES"

# ── Requirement-strength vocabulary (SQL Architecture Correction, Phase 6,
#    Step 3C) ─────────────────────────────────────────────────────────────
#
# Every MatchedRequirement carries a `strength`, deliberately SEPARATE from
# `concept_kind`: kind says WHAT TYPE of concept this is (entity/measure/
# dimension/date-role/identifier); strength says HOW CONFIDENTLY the
# question actually expressed it as something a correct answer must
# satisfy. A real, large schema routinely derives dozens of low-value
# single-word entity-name fragments and generic column-name tokens; without
# this distinction, core.sqlservice.join_planner had no way to tell "the
# question's real ask" from "an incidental word overlap with an unrelated
# table's own derived vocabulary" (see this module's own Step 3C section
# below for the exact mechanism this fixes).
#
#   STRENGTH_CORE: an explicitly expressed business entity, measure,
#       dimension, or date target — see _classify_entity_strength /
#       _classify_column_concept_strength for the exact, generic
#       (never schema-specific) admission rules. Only CORE requirements
#       count as core.sqlservice.join_planner.REQUIREMENT_ROLE_
#       BUSINESS_REQUIRED.
#   STRENGTH_CONSTRAINT: an explicit filter/group/date modifier associated
#       with a core concept (e.g. an "active" status keyword, or a date-role
#       keyword the question did not turn into a trend target) — maps to
#       REQUIREMENT_ROLE_FILTER_OR_GROUPING.
#   STRENGTH_SUPPORTING: weak token/column/schema evidence — retained for
#       ranking and join-path search, never mandatory plan coverage — maps
#       to REQUIREMENT_ROLE_JOIN_HINT (the same "evidence, not coverage"
#       treatment identifiers already received in Step 3B).
#   STRENGTH_UNRESOLVED: a plausible meaning the evidence cannot confidently
#       assign to one candidate — retained for ambiguity/clarification
#       observability only.
STRENGTH_CORE = "CORE"
STRENGTH_CONSTRAINT = "CONSTRAINT"
STRENGTH_SUPPORTING = "SUPPORTING"
STRENGTH_UNRESOLVED = "UNRESOLVED"

# Generic English words that describe an OPERATION or a bare, contentless
# noun for "some data" rather than a business entity — requirement 4's own
# "generic words... cannot become business entities solely through weak
# schema matches". Deliberately generic English vocabulary, never a
# customer/schema-specific term (no object/table name from any real
# customer appears here, by construction — this list would read identically
# for any schema).
_GENERIC_NON_ENTITY_TERMS = frozenset({
    "data", "record", "records", "information", "info", "report", "reports",
    "result", "results", "detail", "details", "item", "items", "entry", "entries",
    "thing", "things", "value", "values", "number", "numbers",
    "count", "list", "show", "display", "give", "find", "get", "tell", "total",
})

_ELIGIBLE_TIERS = frozenset({1, 2})

# Only used to key core.sqlservice.canonical_scoring.compute_canonical_scores'
# result identity field — the function itself is pure and never touches this
# identity for scoring math (mirrors core.sqlservice.semantic_catalog's own
# _CATALOG_CANONICAL_IDENTITY convention exactly).
_CANONICAL_IDENTITY = CanonicalScoringIdentity(
    build_id=0, embedding_provider="schema_linker", embedding_model="none", embedding_dimensions=0,
)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


# ── Evidence-fusion formula (documented, bounded, never tuned per case) ───
#
# combined_score = weighted sum of five INDEPENDENTLY bounded [0,1] terms,
# weights summing to 1.0 — the SAME "no single component dominates" design
# core.sqlservice.canonical_scoring.COMPONENT_WEIGHTS already establishes
# for per-build authority scoring, applied here to per-question anchor
# scoring instead.
#
# Phase 6, Step 3F, Part 2 — weight ORDER now encodes the generic evidence
# reliability hierarchy (never a per-schema/per-word tune, applied
# identically regardless of catalog content):
#   1. catalog evidence (an exact question phrase/business-name match, or
#      a column-derived category-keyword hit) — the largest weight, so an
#      object strongly supported by a matched catalog concept is always
#      findable regardless of vector/lexical rank.
#   2. lexical evidence (an exact normalized token/BM25F match) — ranks
#      above example/semantic: a literal word-overlap match is direct,
#      reproducible evidence, not a learned similarity.
#   3. trusted compatible-example evidence — a previously-validated SQL
#      pattern is real corroboration, but weaker than a literal textual
#      match on THIS question.
#   4. semantic/vector similarity — useful for RECALL (surfacing an object
#      no exact word overlaps with) but, weighted smallest of the four
#      "real evidence" terms, can never by itself outweigh an exact
#      catalog or lexical match (0.30/0.22 each alone already exceed
#      semantic's own 0.16 maximum contribution).
#   5. canonical quality (per-build structural authority) — smallest
#      weight: a tie-break signal only, never a primary relevance source
#      (mirrors core.sqlservice.join_planner's own centrality-last rule).
#
# SQL Architecture Correction, Phase 6, Step 4B1, Part 5 — a sixth,
# independently bounded [0,1] term, ANCHOR_WEIGHT_MODEL_SEMANTIC, is added
# for validated model-inferred alias/business-label/column-role evidence
# (see _score_all_eligible_objects' own model_semantic_score, computed
# SEPARATELY from catalog_score so the two are never conflated). Its own
# raw score is already hard-capped at MODEL_CANDIDATE_CONFIDENCE_CAP (0.50,
# never 1.0 like every other term here), and its WEIGHT is additionally the
# smallest of all six — "do not let the 0.50 model score dominate confirmed
# evidence" (Part 5) is therefore enforced twice over: once by the score's
# own ceiling, once by its own small weight. The five pre-existing weights
# are proportionally rescaled (their RELATIVE ORDER and ratios to each
# other are exactly unchanged — only reduced by the same 0.92 factor) so
# the six terms still sum to 1.0; this is a pure rescale, never a
# re-ranking of the five original evidence sources against each other.
ANCHOR_WEIGHT_MODEL_SEMANTIC = 0.08
_PRE_MODEL_WEIGHT_SCALE = 1.0 - ANCHOR_WEIGHT_MODEL_SEMANTIC
ANCHOR_WEIGHT_CATALOG = 0.30 * _PRE_MODEL_WEIGHT_SCALE
ANCHOR_WEIGHT_LEXICAL = 0.22 * _PRE_MODEL_WEIGHT_SCALE
ANCHOR_WEIGHT_EXAMPLE = 0.18 * _PRE_MODEL_WEIGHT_SCALE
ANCHOR_WEIGHT_SEMANTIC = 0.16 * _PRE_MODEL_WEIGHT_SCALE
ANCHOR_WEIGHT_CANONICAL = 0.14 * _PRE_MODEL_WEIGHT_SCALE
assert abs(
    ANCHOR_WEIGHT_CATALOG + ANCHOR_WEIGHT_SEMANTIC + ANCHOR_WEIGHT_LEXICAL
    + ANCHOR_WEIGHT_CANONICAL + ANCHOR_WEIGHT_EXAMPLE + ANCHOR_WEIGHT_MODEL_SEMANTIC - 1.0
) < 1e-9

# A semantic/lexical RANK (1-indexed, smaller is better) is converted to a
# bounded [0,1] score via linear decay to 0 at this many ranks out — never
# an unbounded raw score, and a rank beyond this saturation point (or no
# rank at all — the object never appeared in that ranking) simply
# contributes 0 to that ONE term, never excluding the object from
# consideration entirely (its catalog/canonical/example terms are computed
# independently and can still make it a strong anchor).
RANK_SATURATION = 40

# Two anchors (or two candidates competing for the same matched concept)
# within this combined_score margin of each other are both treated as live
# possibilities — mirrors core.sqlservice.semantic_catalog._AMBIGUITY_
# CONFIDENCE_MARGIN's exact convention and value.
ANCHOR_AMBIGUITY_MARGIN = 0.15


def _rank_to_bounded_score(rank: int | None, *, saturation: int = RANK_SATURATION) -> float:
    if rank is None:
        return 0.0
    return _clip01(1.0 - (rank - 1) / saturation)


def _noisy_or(confidences: Sequence[float]) -> float:
    """Bounded [0,1] combination of independent evidence confidences — the
    standard "noisy-OR" formula: P(at least one signal is right) assuming
    independence. A single weak confidence contributes only itself
    (noisy_or([0.3]) == 0.3 — never inflated); several corroborating
    weak signals compound (noisy_or([0.3, 0.3, 0.3]) ~= 0.657) without any
    tunable per-source weight. Empty input is 0.0 (no evidence at all)."""
    if not confidences:
        return 0.0
    product = 1.0
    for c in confidences:
        product *= (1.0 - _clip01(c))
    return _clip01(1.0 - product)


# ── Public data model (frozen, deeply immutable) ──────────────────────────


@dataclass(frozen=True)
class RequirementCandidate:
    """One concrete (object, column) — or, for an ENTITY requirement, plain
    object — proposed to satisfy a MatchedRequirement. `column_name` is
    None for ENTITY-kind candidates. `technical_label` is the underlying
    catalog classification (e.g. "FOREIGN_KEY" for an identifier, "amount"
    for a measure, or the entity's own normalized_name) — never part of
    the requirement's own identity (see MatchedRequirement.concept_key).
    `confidence` is already informativeness-adjusted (see `informativeness`
    and MatchedRequirement's own docstring); `raw_confidence` is the
    pre-adjustment catalog value, kept for observability."""

    object_fqn: str
    column_name: str | None
    technical_label: str
    match_kind: str  # MATCH_KIND_*
    confidence: float
    raw_confidence: float
    informativeness: float  # 1.0 == not down-weighted at all
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class MatchedRequirement:
    """
    ONE normalized business concept the question expressed — Phase 6, Step
    2B's core fix. `concept_key` is a STABLE identity built ONLY from
    `concept_kind` + `normalized_label` (see _requirement_key) — it NEVER
    includes an object FQN or column name, so the same business word
    matching the same kind of concept across many different tables (e.g.
    "class" matching a "ClassID" identifier on 50 different tables) is
    represented as ONE requirement with 50 candidates underneath, not 50
    independent requirements. Object/column ownership is fully preserved —
    inside `candidates`, never lost.

    `confidence` is the best candidate's own confidence — computed ONLY over
    STRUCTURAL/dictionary candidates when at least one exists (see
    _build_requirement's tiered sort), so adding model-inferred candidates
    to an existing requirement never changes it. `ambiguous` is True when
    2+ STRUCTURAL candidates remain materially plausible alternatives (see
    _materially_plausible_competitors) — mirrors core.sqlservice.
    semantic_catalog.BusinessEntity.ambiguous's exact convention, and is
    likewise never affected by model-only competition (see
    `model_ambiguous` for that, a completely separate signal — Phase 6,
    Step 4B1, Part 4: "do not turn model-only competition into a new
    mandatory requirement").
    """

    concept_kind: str  # CONCEPT_KIND_*
    concept_key: str
    normalized_label: str
    matched_terms: tuple[str, ...]  # every distinct question term/phrase that produced this requirement
    candidates: tuple[RequirementCandidate, ...]  # bounded (max_candidates_per_requirement); structural tier first, then model tier — each sorted desc confidence
    candidate_total_count: int  # before bounding
    confidence: float
    ambiguous: bool
    evidence: tuple[EvidenceItem, ...]  # top candidate's own evidence, for a quick requirement-level summary
    # Phase 6, Step 3C — see STRENGTH_* constants' own docstring. Determines
    # core.sqlservice.join_planner's REQUIREMENT_ROLE_* mapping; never used
    # by this module's own matching/scoring math above.
    strength: str = STRENGTH_SUPPORTING
    # Phase 6, Step 4B1, Part 4 — True when 2+ MATCH_KIND_MODEL_SEMANTIC
    # candidates (only) are materially plausible alternatives to each
    # other. Completely independent of `ambiguous` above: a requirement can
    # have a perfectly unambiguous structural winner (ambiguous=False) while
    # still carrying competing model-only proposals underneath it
    # (model_ambiguous=True) — advisory information only, never itself a
    # reason to add a requirement or change `strength`.
    model_ambiguous: bool = False


@dataclass(frozen=True)
class QuestionRequirements:
    """Structured semantic requirements extracted from one question. Never
    carries the raw question text itself — only already-matched, already-
    bounded structured fields. Each of the five concept-kind fields is now
    a tuple of MatchedRequirement (Phase 6, Step 2B) — one entry per
    DISTINCT normalized business concept, never one per (object, column)."""

    operation: str  # OPERATION_*
    entity_concepts: tuple[MatchedRequirement, ...]
    measure_concepts: tuple[MatchedRequirement, ...]
    dimension_concepts: tuple[MatchedRequirement, ...]
    date_role_concepts: tuple[MatchedRequirement, ...]
    identifier_concepts: tuple[MatchedRequirement, ...]
    wants_comparison: bool
    wants_trend: bool
    wants_grouping: bool
    unmatched_terms: tuple[str, ...]
    unresolved_filter_values: tuple[str, ...]
    ambiguity_signals: tuple[str, ...]  # short "KIND:label" strings — full detail is SchemaLinkingResult.ambiguities


@dataclass(frozen=True)
class AnchorCandidate:
    """One object proposed as a schema anchor for this question. Every
    score is independently bounded [0, 1] (see the module-level evidence-
    fusion formula); `confidence` is an alias of `combined_score` kept as
    its own named field to match this Step's required output shape.
    `supporting_concepts` now holds normalized MatchedRequirement.
    concept_key values (Phase 6, Step 2B) — never one entry per column.
    `model_semantic_score` (Phase 6, Step 4B1, Part 5) is the noisy-OR of
    ONLY this object's MATCH_KIND_MODEL_SEMANTIC requirement-candidate
    confidences — computed and reported entirely SEPARATELY from
    `catalog_score` (which remains structural/dictionary-only, byte-
    identical to before this step) so a caller can always see exactly how
    much of an anchor's ranking came from validated model evidence."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    supporting_concepts: tuple[str, ...]  # MatchedRequirement.concept_key values this object supports
    supporting_columns: tuple[str, ...]
    semantic_score: float | None
    lexical_score: float | None
    catalog_score: float
    example_score: float
    canonical_score: float
    model_semantic_score: float
    combined_score: float
    evidence: tuple[EvidenceItem, ...]
    confidence: float
    # Phase 6, Step 2A — the exact question-relevance signal(s) (ADMISSION_
    # REASON_*) that qualified this object for consideration at all. Never
    # empty for a returned anchor (canonical_score alone can never be the
    # sole entry, and never appears here at all — see that constant's own
    # docstring).
    admission_reasons: tuple[str, ...]


@dataclass(frozen=True)
class AmbiguityRecord:
    """One deterministic ambiguity signal. `description` references only
    already-matched concept labels/object FQNs — never the raw question
    text (see this module's own docstring on question privacy)."""

    kind: str  # AMBIGUITY_*
    description: str
    candidate_refs: tuple[str, ...]
    confidences: tuple[float, ...]


@dataclass(frozen=True)
class SchemaLinkingLimits:
    """Defensive, configuration-backed bounds — every one truncates
    deterministically (highest-confidence/highest-score kept) and reports
    the truncation via SchemaLinkingResult.warnings; nothing is ever
    silently dropped. `max_anchor_candidates` is an OUTPUT bound only — see
    link_question_to_schema's own docstring for why the complete eligible
    schema is still considered before it is applied."""

    max_question_characters: int = SQL_LINKING_MAX_QUESTION_CHARACTERS
    max_matched_concepts: int = SQL_LINKING_MAX_MATCHED_CONCEPTS  # max MatchedRequirement entries kept PER concept kind
    max_anchor_candidates: int = SQL_LINKING_MAX_ANCHOR_CANDIDATES
    max_evidence_items_per_anchor: int = SQL_LINKING_MAX_EVIDENCE_ITEMS_PER_ANCHOR
    max_ambiguity_records: int = SQL_LINKING_MAX_AMBIGUITY_RECORDS
    max_warnings: int = SQL_LINKING_MAX_WARNINGS
    max_unmatched_terms: int = SQL_LINKING_MAX_UNMATCHED_TERMS
    # Phase 6, Step 2A — relevance-QUALIFICATION thresholds (never an
    # admission signal on their own for canonical_score — see this
    # module's own "Admission-evidence vocabulary" section). Conservative,
    # generic, config-backed defaults; never tuned against any specific
    # schema or question.
    min_semantic_score: float = SQL_LINKING_MIN_SEMANTIC_SCORE
    max_lexical_admission_rank: int = SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK
    min_example_similarity: float = SQL_LINKING_MIN_EXAMPLE_SIMILARITY
    min_anchor_confidence: float = SQL_LINKING_MIN_ANCHOR_CONFIDENCE
    min_ambiguity_confidence: float = SQL_LINKING_MIN_AMBIGUITY_CONFIDENCE
    # Phase 6, Step 2B — normalized-requirement bounds.
    max_candidates_per_requirement: int = SQL_LINKING_MAX_CANDIDATES_PER_REQUIREMENT
    max_ambiguity_alternatives_per_requirement: int = SQL_LINKING_MAX_AMBIGUITY_ALTERNATIVES_PER_REQUIREMENT
    common_token_informativeness_floor: float = SQL_LINKING_COMMON_TOKEN_INFORMATIVENESS_FLOOR
    # Phase 6, Step 3E, Part 5 — `max_anchor_candidates` is a CEILING, not a
    # target: independent lexical/semantic/example evidence alone (objects
    # with NO matched-concept support at all — see _select_anchors' "Phase
    # 2 non-concept fill") may add at most this many extra anchors beyond
    # whatever concept-coverage already selected. Without this bound, a
    # broad real embedding/lexical ranking routinely admits dozens of
    # tangentially-related objects via SEMANTIC_MATCH/LEXICAL_MATCH alone,
    # and Phase 2 would blindly pad the anchor list all the way to the
    # ceiling regardless of true relevance — "full evidence must not
    # automatically return max_anchor_candidates anchors". A small, generic,
    # config-backed bound (never tuned against any specific schema).
    max_non_concept_anchor_fill: int = SQL_LINKING_MAX_NON_CONCEPT_ANCHOR_FILL


DEFAULT_SCHEMA_LINKING_LIMITS = SchemaLinkingLimits()


@dataclass(frozen=True)
class SchemaLinkingResult:
    """Result of one link_question_to_schema() call. `fingerprint` covers
    only the STRUCTURED result content (requirements/anchors/ambiguities) —
    never the raw question text, so it is always safe to log or persist on
    its own (see this module's own docstring)."""

    status: str  # STATUS_OK | STATUS_EMPTY_SCHEMA | STATUS_NO_CONFIDENT_ANCHOR
    requirements: QuestionRequirements
    anchors: tuple[AnchorCandidate, ...]
    ambiguities: tuple[AmbiguityRecord, ...]
    warnings: tuple[str, ...]
    fingerprint: str
    eligible_object_count: int
    considered_object_count: int
    anchor_total_count: int  # candidates that supported >=1 requirement OR had nonzero evidence, before the output bound
    elapsed_ms: float  # == index_load_or_build_ms + question_linking_ms (total wall time of this call)
    # Phase 6, Step 2A — admission/ranking observability (all additive,
    # default-free — every field is always populated on every STATUS_OK/
    # STATUS_NO_CONFIDENT_ANCHOR result).
    relevance_qualified_object_count: int  # eligible objects with >=1 real question-relevance signal
    rejected_for_no_relevance_count: int  # eligible_object_count - relevance_qualified_object_count
    anchor_selected_count: int  # == len(anchors); kept as its own named field per this Step's required shape
    anchors_rejected_by_confidence: int  # selected-but-below min_anchor_confidence, cut before output
    anchor_ceiling_reached: bool  # True iff selection produced MORE candidates than max_anchor_candidates
    no_confident_anchor_reason: str | None  # populated only when status == STATUS_NO_CONFIDENT_ANCHOR
    # Phase 6, Step 2B — normalized-requirement observability.
    raw_catalog_match_count: int  # total (kind, token, object, column) hits observed before aggregation
    normalized_requirement_count: int  # total distinct MatchedRequirement entries across all 5 concept kinds
    duplicate_matches_merged_count: int  # raw_catalog_match_count - total (unbounded) candidates actually kept
    common_token_matches_downweighted_count: int  # final candidates whose informativeness < 1.0
    candidate_alternatives_retained_count: int  # extra candidates kept by ambiguity-margin preservation
    anchors_selected_by_independent_evidence_count: int  # anchors admitted via lexical/semantic/example (not concept-only)
    anchors_retained_for_ambiguity_count: int  # final anchors that came from ambiguity-margin preservation
    # Phase 6, Step 3C — requirement-strength observability (requirement 9).
    # Every count is over the FINAL, bounded requirement set (the same one
    # QuestionRequirements' five tuples expose) — never the raw, pre-
    # aggregation match count.
    core_requirement_count: int = 0
    constraint_requirement_count: int = 0
    supporting_requirement_count: int = 0
    unresolved_requirement_count: int = 0
    substring_suppressed_count: int = 0  # entity matches demoted: redundant substring of a stronger CORE phrase
    weak_single_token_demoted_count: int = 0  # single-token entity matches that never reached CORE at all
    # Phase 6, Step 3E, Part 1 — explicit timing split: schema-derived index
    # construction (cold) or cache lookup (warm) versus this question's own
    # matching/scoring work. A cache hit/miss must only ever change
    # `index_load_or_build_ms` — never the structural (requirements/anchors/
    # ambiguities) result, and never `question_linking_ms`'s own cost.
    index_load_or_build_ms: float = 0.0
    question_linking_ms: float = 0.0
    # Phase 6, Step 3F, Part 1 — see _compute_requirement_fingerprint's own
    # docstring: identical across catalog/lexical/vector/example modes for
    # the same (question, catalog); never derived from the raw question
    # text, anchors, candidates, or evidence-driven scores.
    requirement_fingerprint: str = ""
    # Phase 6, Step 6I, Part 2 — count of SUPPORTING-strength requirements
    # whose concept_key was promoted into anchor-mandatory coverage by
    # _qualifying_supporting_concept_refs (see that function's own
    # docstring for the full eligibility bar). Zero whenever no SUPPORTING
    # requirement's evidence was strong/direct/informative enough to
    # qualify — the common case.
    supporting_anchor_promotions_count: int = 0
    # Phase 6, Step 6J, Part 1 fix — the ACTUAL promoted concept_key set
    # (never just its count), so a downstream consumer (core.sqlservice.
    # join_planner) can tell WHICH concepts this module already decided
    # deserve anchor-mandatory treatment. A real audit proved the promotion
    # above was previously "half-wired": schema_linker already promoted a
    # strong SUPPORTING candidate to an anchor, but core.sqlservice.
    # join_planner's own REQUIREMENT_ROLE mapping had no way to see that
    # decision at all and mapped strength==SUPPORTING to REQUIREMENT_ROLE_
    # JOIN_HINT unconditionally regardless — earning the promoted concept
    # zero mandatory-coverage scoring credit even after it correctly became
    # an anchor. This field is the fix's own data channel; the concept's
    # own `strength` is NEVER changed (stays SUPPORTING) — only which
    # planner ROLE it maps to, downstream, changes.
    promoted_supporting_concept_keys: frozenset[str] = frozenset()


# ── Local helpers ──────────────────────────────────────────────────────────


def _bound(items: Sequence, max_items: int, *, label: str, warnings: list[str]) -> tuple[tuple, int]:
    kept = tuple(items[:max_items])
    omitted = max(0, len(items) - max_items)
    if omitted:
        warnings.append(f"{label}: {omitted} item(s) truncated by max_items={max_items}")
    return kept, omitted


def _meaningful_question_tokens(tokens: Sequence[str]) -> tuple[str, ...]:
    return tuple(t for t in tokens if len(t) >= 3 and any(c.isalpha() for c in t) and not t.isdigit())


def _requirement_key(concept_kind: str, normalized_label: str) -> str:
    """The ONLY inputs to a requirement's stable identity: concept kind and
    normalized business meaning — deliberately never an object FQN or
    column name/position (Phase 6, Step 2B, requirement 1)."""
    return f"{concept_kind}:{normalized_label.lower()}"


# ── Operation / intent detection (generic English phrase patterns) ────────
#
# Checked in this fixed priority order — first match wins for `operation`;
# wants_comparison/wants_trend/wants_grouping are independent booleans
# checked separately (a question can be, e.g., both GROUPED_AGGREGATION and
# wants_trend=True at once — "total sales by month").

_OPERATION_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # "how many"-shaped questions are checked FIRST — "how many X are
    # there" would otherwise also match the EXISTENCE phrase "are there"
    # below; a count question always wins over a bare existence phrase.
    (OPERATION_DISTINCT_COUNT, re.compile(
        r"\bdistinct\b|\bunique\b|\bhow many different\b", re.IGNORECASE,
    )),
    (OPERATION_FILTERED_COUNT, re.compile(
        r"\bhow many\b.*\b(who|that|with|where|in|from)\b|\bcount of\b.*\b(who|that|with|where)\b",
        re.IGNORECASE,
    )),
    (OPERATION_ROW_COUNT, re.compile(r"\bhow many\b|\bcount of\b|\bnumber of\b", re.IGNORECASE)),
    (OPERATION_RANKING_TOP_N, re.compile(
        r"\btop\s+\d+\b|\bbottom\s+\d+\b|\brank(ed|ing)?\b", re.IGNORECASE,
    )),
    (OPERATION_TREND, re.compile(
        r"\bover time\b|\btrend\b|\bby (month|year|week|day|quarter)\b|\bmonthly\b|\byearly\b|"
        r"\bhistorical(ly)?\b|\btime series\b|\bover the (last|past)\b",
        re.IGNORECASE,
    )),
    (OPERATION_COMPARISON, re.compile(
        r"\bcompare[ds]?\b|\bversus\b|\bvs\.?\b|\bdifference between\b|\brelative to\b", re.IGNORECASE,
    )),
    (OPERATION_AVERAGE, re.compile(r"\baverage\b|\bavg\b|\bmean\b", re.IGNORECASE)),
    (OPERATION_MAXIMUM, re.compile(r"\b(max(imum)?|highest|largest|greatest|most)\b", re.IGNORECASE)),
    (OPERATION_MINIMUM, re.compile(r"\b(min(imum)?|lowest|smallest|least)\b", re.IGNORECASE)),
    (OPERATION_SUM, re.compile(r"\btotal\b|\bsum\b|\baltogether\b", re.IGNORECASE)),
    (OPERATION_GROUPED_AGGREGATION, re.compile(
        r"\bfor each\b|\bper\s+\w+\b|\bbroken down by\b|\bgrouped?\s+by\b|\bbreakdown\b", re.IGNORECASE,
    )),
    # Existence is checked LAST among the "count-shaped" family — only a
    # bare "is/are there"/"does X exist" with no count phrasing at all
    # reaches here.
    (OPERATION_EXISTENCE, re.compile(
        r"\b(is|are|was|were)\s+there\b|\bdoes\s+\w+(\s+\w+){0,3}\s+exist\b|\bany\s+\w+\s+(that|who|which)\b",
        re.IGNORECASE,
    )),
    (OPERATION_LIST_DETAIL, re.compile(
        r"\blist\b|\bshow\b|\bdisplay\b|\bwhich\b|\bwho\b|\bwhat are\b|\bgive me\b", re.IGNORECASE,
    )),
)

_COMPARISON_PATTERN = re.compile(r"\bcompare[ds]?\b|\bversus\b|\bvs\.?\b|\bdifference between\b|\brelative to\b", re.IGNORECASE)
_TREND_PATTERN = re.compile(
    r"\bover time\b|\btrend\b|\bby (month|year|week|day|quarter)\b|\bmonthly\b|\byearly\b|"
    r"\btime series\b|\bover the (last|past)\b",
    re.IGNORECASE,
)
_GROUPING_PATTERN = re.compile(
    r"\bby\s+\w+\b|\bper\s+\w+\b|\bfor each\b|\bgrouped?\s+by\b|\bbroken down by\b|\bbreakdown\b", re.IGNORECASE,
)


def _detect_operation(raw_question_lower: str) -> str:
    for operation, pattern in _OPERATION_PATTERNS:
        if pattern.search(raw_question_lower):
            return operation
    return OPERATION_UNKNOWN


# ── Value/filter-term extraction (never validated against real data) ─────

_QUOTED_VALUE_PATTERN = re.compile(r'"([^"]{1,100})"|\'([^\']{1,100})\'')
_VALUE_INTRODUCER_PATTERN = re.compile(
    r"\b(?:named|called|titled|labell?ed|equals?)\s+([A-Za-z][\w\-]*(?:\s+[A-Za-z][\w\-]*){0,3})",
    re.IGNORECASE,
)
_BARE_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")
_RANKING_NUMBER_CONTEXT = re.compile(r"\b(top|bottom)\s+\d+", re.IGNORECASE)


def _extract_unresolved_filter_values(raw_question: str) -> tuple[str, ...]:
    """
    Likely literal filter values — quoted substrings, "named X"/"called X"-
    style introduced phrases, and bare numeric literals not already
    consumed by a "top N"/"bottom N" ranking phrase. Never validated
    against any real data (this step never queries a database) and never
    claimed to be a KNOWN value — just preserved for a later stage.
    """
    values: list[str] = []
    for match in _QUOTED_VALUE_PATTERN.finditer(raw_question):
        value = match.group(1) or match.group(2)
        if value:
            values.append(value.strip())
    for match in _VALUE_INTRODUCER_PATTERN.finditer(raw_question):
        values.append(match.group(1).strip())

    ranking_spans = [m.span() for m in _RANKING_NUMBER_CONTEXT.finditer(raw_question)]
    for match in _BARE_NUMBER_PATTERN.finditer(raw_question):
        start, end = match.span()
        if any(rs <= start < re_ for rs, re_ in ranking_spans):
            continue
        values.append(match.group(0))

    seen: set[str] = set()
    unique: list[str] = []
    for v in values:
        key = v.lower()
        if key not in seen:
            seen.add(key)
            unique.append(v)
    return tuple(unique)


# ── Catalog-derived lookup index (cached by SemanticCatalog.fingerprint) ──


@dataclass(frozen=True)
class _EntityIndexEntry:
    entity: BusinessEntity


@dataclass(frozen=True)
class _ColumnConceptEntry:
    concept_kind: str
    object_fqn: str
    column_name: str
    label: str
    base_confidence: float
    evidence: tuple[EvidenceItem, ...]


# ── Model-inferred advisory index entries (Phase 6, Step 4B1) ─────────────
#
# Deliberately SEPARATE dataclasses from _EntityIndexEntry/_ColumnConceptEntry
# above, never reusing or subclassing them — every field here traces back
# ONLY to core.sqlservice.semantic_catalog.SemanticCatalog.model_inferred_
# object_evidence/model_inferred_column_evidence, never to a BusinessEntity/
# MeasureConcept/DimensionConcept/DateRoleConcept/IdentifierConcept (real
# structural/dictionary concepts), so the two evidence classes can never be
# confused with each other downstream — the caller can always tell "this
# came from real schema-document structure" from "this came from validated
# model inference" purely from WHICH entry type produced it, before even
# looking at match_kind/provenance.


@dataclass(frozen=True)
class _ModelEntityIndexEntry:
    """One REAL object a validated model-inferred business_name/alias/
    entity_concept candidate maps to an ALREADY-EXISTING entity concept
    (see model_entity_alias_index's own docstring — this entry is never
    created for an object/concept the catalog's real `entities` don't
    already contain)."""

    object_fqn: str
    confidence: float  # already re-capped at MODEL_CANDIDATE_CONFIDENCE_CAP AND informativeness-discounted
    informativeness: float  # 1.0 == not down-weighted at all (see _build_model_entity_alias_index)
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class _ModelColumnConceptEntry:
    """One REAL column a validated model-inferred measure/dimension/date-
    role/identifier role maps to — structurally verified against this
    build's own documents (see _column_type_and_flags) BEFORE this entry is
    ever created; a contradicting real type/PK/FK fact means no entry is
    created at all (Part 3: "inferred roles cannot override contradictory
    type metadata"), never a confidence penalty alone."""

    concept_kind: str
    object_fqn: str
    column_name: str
    label: str
    confidence: float  # already re-capped and, for an unknown/OTHER real type, down-weighted
    evidence: tuple[EvidenceItem, ...]


@dataclass(frozen=True)
class _LinkerIndex:
    """Every structure derived ONLY from (semantic_catalog, documents) —
    never from a question. Built once per distinct SemanticCatalog.
    fingerprint and cached (see _load_linker_index)."""

    eligible_fqns_lower: frozenset[str]
    fqn_by_lower: Mapping[str, str]
    object_type_by_fqn_lower: Mapping[str, str]
    tier_by_fqn_lower: Mapping[str, int]
    canonical_score_by_fqn_lower: Mapping[str, float]
    entity_exact_token_index: Mapping[str, tuple[_EntityIndexEntry, ...]]  # normalized_name -> entities
    entity_alias_token_index: Mapping[str, tuple[_EntityIndexEntry, ...]]  # single-token alias -> entities
    entity_alias_phrase_index: Mapping[str, tuple[_EntityIndexEntry, ...]]  # multi-word alias phrase -> entities
    column_token_index: Mapping[str, tuple[_ColumnConceptEntry, ...]]
    ambiguous_entities: tuple[BusinessEntity, ...]
    family_key_by_fqn_lower: Mapping[str, str]  # object_fqn.lower() -> canonical_scoring family_key, where known
    # Phase 6, Step 2B — bounded IDF-like informativeness, NOT yet floored
    # (the floor is a per-call SchemaLinkingLimits value, applied at
    # question time — see _token_informativeness) — 1.0 for a token unique
    # to a single object, decreasing toward 0 (before flooring) as more
    # DISTINCT objects share it. Computed once from column_token_index's
    # own structural document frequency — never a hardcoded token list.
    token_normalized_idf: Mapping[str, float]
    # Phase 6, Step 3C — the SAME bounded IDF-like shape as
    # `token_normalized_idf` above, but scoped to entity ALIAS TOKENS
    # (single-word, never phrases — see entity_alias_token_index) and
    # counted by DISTINCT ENTITY, not distinct object: 1.0 for an alias
    # token that names only one entity's alias list, decreasing toward 0 as
    # more DISTINCT entities happen to share it as an incidental alias
    # fragment. This is exactly the signal that separates a genuine business
    # word (e.g. an entity's own name) from an incidental cross-entity
    # vocabulary artifact (e.g. a shared word every table under one
    # functional area happened to inherit as a synonym) — see
    # _classify_entity_strength.
    entity_alias_token_normalized_idf: Mapping[str, float]
    # Phase 6, Step 4B1, Part 2 — normalized alias/business-name/entity-
    # concept STRING (whole phrase lowercased+stripped, and independently
    # each of its own single tokens) -> the real object(s) validated model
    # evidence proposed for it. Looked up ONLY by an EXISTING entity's own
    # `normalized_name.lower()` at match time (see _match_entities) — this
    # index is never itself scanned against question tokens, so it can
    # never seed a requirement that doesn't already exist from structural
    # matching (Part 1: "may only add/rank candidates underneath
    # requirements already derived from question language and deterministic
    # semantics").
    model_entity_alias_index: Mapping[str, tuple[_ModelEntityIndexEntry, ...]]
    # Phase 6, Step 4B1, Part 3 — token -> validated model column-role
    # entries, indexed by tokenize(column_name) and tokenize(role label)
    # exactly like column_token_index above (see _add_model_column_entry).
    # Looked up ONLY for a (concept_kind, token) pair that ALREADY has a
    # real structural group (see _match_column_concepts) — same "never
    # seeds a new requirement" guarantee as model_entity_alias_index.
    model_column_token_index: Mapping[str, tuple[_ModelColumnConceptEntry, ...]]


def _index_measure(concept: MeasureConcept) -> _ColumnConceptEntry:
    return _ColumnConceptEntry(
        concept_kind=CONCEPT_KIND_MEASURE, object_fqn=concept.object_fqn, column_name=concept.column_name,
        label=concept.normalized_measure, base_confidence=concept.confidence, evidence=concept.evidence,
    )


def _index_dimension(concept: DimensionConcept) -> _ColumnConceptEntry:
    return _ColumnConceptEntry(
        concept_kind=CONCEPT_KIND_DIMENSION, object_fqn=concept.object_fqn, column_name=concept.column_name,
        label=concept.dimension_kind, base_confidence=concept.confidence, evidence=concept.evidence,
    )


def _index_date_role(concept: DateRoleConcept) -> tuple[_ColumnConceptEntry, ...]:
    return tuple(
        _ColumnConceptEntry(
            concept_kind=CONCEPT_KIND_DATE_ROLE, object_fqn=concept.object_fqn, column_name=concept.column_name,
            label=candidate.role, base_confidence=candidate.confidence, evidence=candidate.evidence,
        )
        for candidate in concept.candidates
    )


def _index_identifier(concept: IdentifierConcept) -> _ColumnConceptEntry:
    return _ColumnConceptEntry(
        concept_kind=CONCEPT_KIND_IDENTIFIER, object_fqn=concept.object_fqn, column_name=concept.column_name,
        label=concept.identifier_kind, base_confidence=concept.confidence, evidence=concept.evidence,
    )


# ── Model-inferred advisory index construction (Phase 6, Step 4B1) ────────

_MODEL_DATE_COMPATIBLE_TYPES = frozenset({"DATETIME"})
_MODEL_MEASURE_COMPATIBLE_TYPES = frozenset({"INTEGER", "DECIMAL"})
_MODEL_UNKNOWN_TYPE_TOKEN = "OTHER"
# An unknown/legacy real type is never proof of incompatibility (Part 3:
# "unknown/legacy OTHER types remain lower-confidence" — never a hard
# reject) — halved relative to a type-confirmed match, mirroring core.
# sqlservice.semantic_catalog's own "unknown types preserve low-confidence
# fallback rather than fabricating certainty" convention.
_MODEL_UNKNOWN_TYPE_CONFIDENCE_MULTIPLIER = 0.5

_MODEL_COLUMN_ROLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("measure_role_candidates", CONCEPT_KIND_MEASURE),
    ("dimension_role_candidates", CONCEPT_KIND_DIMENSION),
    ("date_role_candidates", CONCEPT_KIND_DATE_ROLE),
    ("identifier_role_candidates", CONCEPT_KIND_IDENTIFIER),
)


def _column_type_and_flags(metadata: Mapping[str, object]) -> Mapping[str, tuple[str, bool, bool]]:
    """
    column_name.lower() -> (normalized_sql_type, is_primary_key,
    is_foreign_key), read directly from this document's OWN metadata —
    never a database call. Mirrors core.sqlservice.semantic_enrichment_
    service's identical column_descriptors-else-legacy-fallback contract,
    duplicated here rather than imported: this module must never depend on
    core.sqlservice.semantic_enrichment_service/_store at all (a layering
    violation this module's own docstring already forbids for every other
    catalog/document consumer here), and must keep working unchanged on a
    document set that predates enrichment entirely.
    """
    lookup: dict[str, tuple[str, bool, bool]] = {}
    for descriptor in metadata.get("column_descriptors") or ():
        if not isinstance(descriptor, Mapping):
            continue
        name = descriptor.get("column_name")
        if isinstance(name, str) and name:
            lookup[name.lower()] = (
                str(descriptor.get("normalized_sql_type") or _MODEL_UNKNOWN_TYPE_TOKEN),
                bool(descriptor.get("is_primary_key")), bool(descriptor.get("is_foreign_key")),
            )
    if lookup:
        return lookup

    primary_keys = {str(c).lower() for c in (metadata.get("primary_key_columns") or ()) if isinstance(c, str)}
    foreign_keys = {
        str(edge.get("column")).lower() for edge in (metadata.get("confirmed_relationship_edges") or ())
        if isinstance(edge, Mapping) and edge.get("direction") == "outbound" and isinstance(edge.get("column"), str)
    }
    for name in metadata.get("column_names") or ():
        if isinstance(name, str) and name:
            key = name.lower()
            lookup[key] = (_MODEL_UNKNOWN_TYPE_TOKEN, key in primary_keys, key in foreign_keys)
    return lookup


def _gate_model_column_confidence(
    concept_kind: str, label: str, confidence: float, type_info: tuple[str, bool, bool],
) -> float | None:
    """
    Returns the (possibly down-weighted) confidence to use for one
    validated model column-role entry, or None to REJECT it outright —
    Part 3: "its structural type must remain compatible; inferred roles
    cannot override contradictory type metadata; unknown/legacy OTHER
    types remain lower-confidence". A real, KNOWN, contradicting type/PK/FK
    fact is a hard gate (never a mere confidence penalty), mirroring core.
    sqlservice.semantic_catalog's own _type_gate/structural-contradiction
    philosophy exactly. `type_info` is only ever called with a REAL,
    already-resolved (normalized_sql_type, is_primary_key, is_foreign_key)
    triple — the caller (_build_model_column_token_index) already rejects
    a column absent from this object's own real column list entirely
    before this function is ever invoked ("unknown column impossible").
    """
    known_type, is_primary_key, is_foreign_key = type_info
    is_unknown_type = known_type == _MODEL_UNKNOWN_TYPE_TOKEN

    if concept_kind == CONCEPT_KIND_DATE_ROLE and not is_unknown_type and known_type not in _MODEL_DATE_COMPATIBLE_TYPES:
        return None
    if concept_kind == CONCEPT_KIND_MEASURE and not is_unknown_type and known_type not in _MODEL_MEASURE_COMPATIBLE_TYPES:
        return None
    if concept_kind == CONCEPT_KIND_IDENTIFIER:
        if label == "PRIMARY_KEY" and not is_primary_key:
            return None
        if label == "FOREIGN_KEY" and not is_foreign_key:
            return None

    return confidence * _MODEL_UNKNOWN_TYPE_CONFIDENCE_MULTIPLIER if is_unknown_type else confidence


def _build_model_entity_alias_index(
    catalog: SemanticCatalog, eligible_fqns_lower: frozenset[str],
    entity_alias_token_normalized_idf: Mapping[str, float],
) -> Mapping[str, tuple[_ModelEntityIndexEntry, ...]]:
    """
    Validated model business_name/alias/entity_concept candidates, indexed
    by every normalized string form (the whole phrase lowercased+stripped,
    and independently each of its own tokens) that matches an ENTITY
    CONCEPT NAME ALREADY PRESENT in `catalog.entities` — Part 1's "may only
    add/rank candidates underneath requirements already derived from
    question language and deterministic semantics" enforced at index-build
    time: a model alias/business-name that doesn't happen to match any
    REAL entity's own `normalized_name` contributes nothing here at all,
    by construction, never merely by convention. `object_evidence.
    object_fqn` is additionally required to be a REAL, eligible object of
    THIS build (`eligible_fqns_lower`, the same set every structural index
    above is scoped to) — defense in depth against a malformed/adversarial
    `enrichment_input` claiming an object that isn't actually part of this
    schema at all ("unknown object impossible"), never merely trusted from
    core.sqlservice.semantic_catalog's own upstream validation.

    Phase 6, Step 6G, Part 3 — a matched SINGLE-TOKEN key's confidence is
    additionally scaled by `entity_alias_token_normalized_idf` (the SAME
    bounded, structural, per-build document-frequency table
    core.sqlservice.semantic_catalog's own real entity-alias matching
    already uses — never a second/duplicate informativeness computation):
    a token many DISTINCT real entities already share as an alias
    fragment is generic regardless of who claims it, so a view whose
    model-inferred aliases happen to reuse a common word gets no more
    credit for it than a real entity alias would. A whole PHRASE key has
    no entry in this token-keyed table and is therefore never discounted
    (`.get(key, 1.0)`) — a multi-word exact phrase match is inherently
    specific, mirroring _classify_entity_strength's own unconditional-CORE
    treatment of ALIAS_PHRASE scope.
    """
    existing_entity_names_lower = {e.normalized_name.lower() for e in catalog.entities}
    raw: dict[str, dict[str, _ModelEntityIndexEntry]] = defaultdict(dict)

    for object_evidence in catalog.model_inferred_object_evidence:
        if object_evidence.object_fqn.lower() not in eligible_fqns_lower:
            continue
        confidence = min(_clip01(object_evidence.confidence), MODEL_CANDIDATE_CONFIDENCE_CAP)
        candidate_strings = (
            tuple(object_evidence.business_name_candidates)
            + tuple(object_evidence.alias_candidates)
            + tuple(object_evidence.entity_concept_candidates)
        )
        matched_keys: set[str] = set()
        for candidate_string in candidate_strings:
            phrase = candidate_string.strip().lower()
            if phrase:
                matched_keys.add(phrase)
            matched_keys.update(tokenize(candidate_string))

        for key in matched_keys:
            if key not in existing_entity_names_lower:
                continue
            informativeness = entity_alias_token_normalized_idf.get(key, 1.0)
            effective_confidence = _clip01(confidence * informativeness)
            by_object = raw[key]
            existing_entry = by_object.get(object_evidence.object_fqn)
            if existing_entry is not None and existing_entry.confidence >= effective_confidence:
                continue
            by_object[object_evidence.object_fqn] = _ModelEntityIndexEntry(
                object_fqn=object_evidence.object_fqn, confidence=effective_confidence,
                informativeness=informativeness,
                evidence=(
                    EvidenceItem(kind=MATCH_KIND_MODEL_SEMANTIC, detail=key),
                    EvidenceItem(kind=PROVENANCE_MODEL_INFERRED_SEMANTIC, detail=object_evidence.object_fqn),
                ),
            )

    return {
        key: tuple(sorted(by_object.values(), key=lambda e: (-e.confidence, e.object_fqn)))
        for key, by_object in raw.items()
    }


def _build_model_column_token_index(
    catalog: SemanticCatalog, documents_by_fqn_lower: Mapping[str, object],
) -> Mapping[str, tuple[_ModelColumnConceptEntry, ...]]:
    """
    Validated model measure/dimension/date-role/identifier-role candidates
    for REAL columns, indexed by tokenize(column_name) and tokenize(role
    label) exactly like the structural column_token_index — Part 3: "the
    real column must exist; its structural type must remain compatible".
    Both are enforced HERE, independently, against THIS build's own
    documents — never merely trusted from core.sqlservice.semantic_
    catalog's own upstream validation ("unknown object/column impossible"
    is a real, re-checked guarantee at this layer too, not merely assumed
    of the caller). A column absent from this object's own real column list
    entirely (`type_info is None`) is a hard reject — distinct from a REAL
    column whose type merely couldn't be classified (a genuine "OTHER"/
    unknown TYPE, which is never a reject, only a down-weight — see
    _gate_model_column_confidence).
    """
    raw: dict[str, dict[tuple[str, str, str], _ModelColumnConceptEntry]] = defaultdict(dict)

    for column_evidence in catalog.model_inferred_column_evidence:
        doc = documents_by_fqn_lower.get(column_evidence.object_fqn.lower())
        if doc is None:
            continue  # unknown object -- impossible, never indexed
        type_info = _column_type_and_flags(doc.metadata).get(column_evidence.column_name.lower())
        if type_info is None:
            continue  # unknown column -- impossible, never indexed (never merely down-weighted)

        for field_name, concept_kind in _MODEL_COLUMN_ROLE_FIELDS:
            for role_label, role_confidence in getattr(column_evidence, field_name):
                capped = min(_clip01(role_confidence), MODEL_CANDIDATE_CONFIDENCE_CAP)
                gated_confidence = _gate_model_column_confidence(concept_kind, role_label, capped, type_info)
                if gated_confidence is None:
                    continue
                entry = _ModelColumnConceptEntry(
                    concept_kind=concept_kind, object_fqn=column_evidence.object_fqn,
                    column_name=column_evidence.column_name, label=role_label, confidence=gated_confidence,
                    evidence=(
                        EvidenceItem(kind=MATCH_KIND_MODEL_SEMANTIC, detail=f"{column_evidence.column_name}:{role_label}"),
                        EvidenceItem(kind=PROVENANCE_MODEL_INFERRED_SEMANTIC, detail=column_evidence.object_fqn),
                    ),
                )
                dedup_key = (concept_kind, column_evidence.object_fqn.lower(), column_evidence.column_name.lower())
                for token in set(tokenize(column_evidence.column_name)) | set(tokenize(role_label)):
                    bucket = raw[token]
                    existing = bucket.get(dedup_key)
                    if existing is not None and existing.confidence >= gated_confidence:
                        continue
                    bucket[dedup_key] = entry

    return {
        token: tuple(sorted(bucket.values(), key=lambda e: (-e.confidence, e.object_fqn, e.column_name)))
        for token, bucket in raw.items()
    }


def _build_linker_index(catalog: SemanticCatalog, documents: Sequence) -> _LinkerIndex:
    sorted_documents = sorted(documents, key=lambda d: d.object_fqn.lower())

    fqn_by_lower: dict[str, str] = {}
    object_type_by_fqn_lower: dict[str, str] = {}
    tier_by_fqn_lower: dict[str, int] = {}
    for doc in sorted_documents:
        key = doc.object_fqn.lower()
        fqn_by_lower[key] = doc.object_fqn
        object_type_by_fqn_lower[key] = doc.object_type
        tier_by_fqn_lower[key] = doc.retrieval_tier

    eligible_fqns_lower = frozenset(k for k, tier in tier_by_fqn_lower.items() if tier in _ELIGIBLE_TIERS)

    canonical_result = compute_canonical_scores(sorted_documents, _CANONICAL_IDENTITY)
    canonical_score_by_fqn_lower = {
        key: score.total_score for key, score in canonical_result.scores_by_fqn_lower.items()
    }

    entity_exact_token_index: dict[str, list[_EntityIndexEntry]] = defaultdict(list)
    entity_alias_token_index: dict[str, list[_EntityIndexEntry]] = defaultdict(list)
    entity_alias_phrase_index: dict[str, list[_EntityIndexEntry]] = defaultdict(list)
    ambiguous_entities: list[BusinessEntity] = []

    for entity in catalog.entities:
        entry = _EntityIndexEntry(entity=entity)
        entity_exact_token_index[entity.normalized_name.lower()].append(entry)
        for alias in entity.aliases:
            alias_lower = alias.lower().strip()
            if not alias_lower:
                continue
            if " " in alias_lower:
                entity_alias_phrase_index[alias_lower].append(entry)
            else:
                entity_alias_token_index[alias_lower].append(entry)
        if entity.ambiguous:
            ambiguous_entities.append(entity)

    column_token_index: dict[str, list[_ColumnConceptEntry]] = defaultdict(list)

    def _add_column_entry(entry: _ColumnConceptEntry) -> None:
        for token in tokenize(entry.column_name):
            column_token_index[token].append(entry)
        for token in tokenize(entry.label):
            column_token_index[token].append(entry)

    for measure in catalog.measures:
        _add_column_entry(_index_measure(measure))
    for dimension in catalog.dimensions:
        _add_column_entry(_index_dimension(dimension))
    for date_role in catalog.date_roles:
        for entry in _index_date_role(date_role):
            _add_column_entry(entry)
    for identifier in catalog.identifiers:
        _add_column_entry(_index_identifier(identifier))

    # Phase 6, Step 2A — canonical-family membership, reused verbatim from
    # core.sqlservice.canonical_scoring via core.sqlservice.semantic_catalog.
    # object_families (never a second family algorithm). Used only to tell
    # apart a genuine competing-object ambiguity from two members of the
    # SAME family (e.g. a table and its own narrow/duplicate variant).
    family_key_by_fqn_lower: dict[str, str] = {}
    for family in catalog.object_families:
        for member_fqn in family.member_object_fqns:
            family_key_by_fqn_lower[member_fqn.lower()] = family.family_key

    # Phase 6, Step 2B — bounded, un-floored normalized IDF per token, from
    # this build's own column_token_index structural document frequency
    # (count of DISTINCT OBJECTS whose column/label tokenizes to this
    # token) — never a hardcoded column-name list. 1.0 at document
    # frequency 1 (a token unique to one object), decreasing toward 0 (the
    # floor is applied later, per-call) as more distinct objects share it.
    token_document_frequency: dict[str, int] = {
        token: len({e.object_fqn.lower() for e in entries})
        for token, entries in column_token_index.items()
    }
    total_eligible = len(eligible_fqns_lower) or 1
    max_idf = math.log1p(total_eligible / 2.0)
    token_normalized_idf: dict[str, float] = {}
    for token, df in token_document_frequency.items():
        idf = math.log1p(total_eligible / (1.0 + df))
        token_normalized_idf[token] = _clip01(idf / max_idf) if max_idf > 0 else 1.0

    # Phase 6, Step 3C — the same normalized-IDF shape, over DISTINCT
    # ENTITIES sharing an alias TOKEN (never a phrase) as an incidental
    # fragment — see entity_alias_token_normalized_idf's own docstring.
    entity_alias_token_document_frequency: dict[str, int] = {
        token: len({e.entity.normalized_name.lower() for e in entries})
        for token, entries in entity_alias_token_index.items()
    }
    total_entities = len(catalog.entities) or 1
    max_entity_idf = math.log1p(total_entities / 2.0)
    entity_alias_token_normalized_idf: dict[str, float] = {}
    for token, df in entity_alias_token_document_frequency.items():
        idf = math.log1p(total_entities / (1.0 + df))
        entity_alias_token_normalized_idf[token] = _clip01(idf / max_entity_idf) if max_entity_idf > 0 else 1.0

    # Phase 6, Step 4B1 — validated model-inferred advisory indexes. Built
    # unconditionally (an empty catalog.model_inferred_*_evidence, the
    # default for any catalog built without enrichment_input, simply
    # produces two empty mappings) so this function has exactly ONE code
    # path, never a special-cased "with enrichment"/"without enrichment"
    # branch.
    model_entity_alias_index = _build_model_entity_alias_index(
        catalog, eligible_fqns_lower, entity_alias_token_normalized_idf,
    )
    documents_by_fqn_lower = {d.object_fqn.lower(): d for d in sorted_documents}
    model_column_token_index = _build_model_column_token_index(catalog, documents_by_fqn_lower)

    return _LinkerIndex(
        eligible_fqns_lower=eligible_fqns_lower,
        fqn_by_lower=MappingProxyType(fqn_by_lower),
        object_type_by_fqn_lower=MappingProxyType(object_type_by_fqn_lower),
        tier_by_fqn_lower=MappingProxyType(tier_by_fqn_lower),
        canonical_score_by_fqn_lower=MappingProxyType(canonical_score_by_fqn_lower),
        entity_exact_token_index=MappingProxyType({k: tuple(v) for k, v in entity_exact_token_index.items()}),
        entity_alias_token_index=MappingProxyType({k: tuple(v) for k, v in entity_alias_token_index.items()}),
        entity_alias_phrase_index=MappingProxyType({k: tuple(v) for k, v in entity_alias_phrase_index.items()}),
        column_token_index=MappingProxyType({k: tuple(v) for k, v in column_token_index.items()}),
        ambiguous_entities=tuple(ambiguous_entities),
        family_key_by_fqn_lower=MappingProxyType(family_key_by_fqn_lower),
        token_normalized_idf=MappingProxyType(token_normalized_idf),
        entity_alias_token_normalized_idf=MappingProxyType(entity_alias_token_normalized_idf),
        model_entity_alias_index=MappingProxyType(model_entity_alias_index),
        model_column_token_index=MappingProxyType(model_column_token_index),
    )


def _token_informativeness(index: _LinkerIndex, token: str, floor: float) -> float:
    """Bounded [floor, 1.0] informativeness for one token — the cached,
    un-floored normalized IDF (see _LinkerIndex.token_normalized_idf),
    floored per-call so a caller's `common_token_informativeness_floor`
    (SchemaLinkingLimits) is always honored without invalidating the
    cached, question-independent index. A token this build never indexed
    at all (should not normally happen for a token that produced a match)
    defaults to 1.0 — never assumed common."""
    return max(floor, index.token_normalized_idf.get(token, 1.0))


# ── Bounded in-process cache — keyed ONLY by SemanticCatalog.fingerprint,
#    NEVER by question content (requirement: "never cache user questions
#    or question-derived results") ─────────────────────────────────────────

_index_cache: "OrderedDict[str, _LinkerIndex]" = OrderedDict()


def _load_linker_index(catalog: SemanticCatalog, documents: Sequence, *, use_cache: bool = True) -> _LinkerIndex:
    key = catalog.fingerprint
    if use_cache:
        cached = _index_cache.get(key)
        if cached is not None:
            _index_cache.move_to_end(key)
            return cached

    index = _build_linker_index(catalog, documents)
    if use_cache:
        _index_cache[key] = index
        _index_cache.move_to_end(key)
        while len(_index_cache) > SQL_LINKING_MAX_CACHED_INDEXES:
            _index_cache.popitem(last=False)
    return index


def prepare_linker_index(catalog: SemanticCatalog, documents: Sequence, *, use_cache: bool = True) -> None:
    """
    Warm this module's own fingerprint-keyed `_LinkerIndex` cache for
    `catalog` — SQL Architecture Correction, Phase 6, Step 4B, Part 6:
    "prepare the existing semantic linker... indexes. No question is
    involved." A thin public wrapper over the already-existing, private
    `_load_linker_index` — no new caching mechanism, no change to how a
    real question-time `link_question_to_schema()` call builds or reuses
    this same cache. Discards the built index; callers only need the
    side effect (a warm cache entry), never the index object itself.

    Cache-invalidation contract (Part 6): the cache key is `catalog.
    fingerprint` (unchanged — see `_load_linker_index` above), so a catalog
    built with NEW/changed core.sqlservice.semantic_catalog.build_semantic_
    catalog(enrichment_input=...) content automatically gets a fingerprint
    that differs from an un-enriched catalog over the same documents (see
    that module's own SemanticCatalog.fingerprint docstring) — this
    function therefore never needs its own enrichment-awareness to satisfy
    "new enrichment cannot accidentally reuse an old un-enriched linker
    index"; it is a direct, automatic consequence of a fingerprint that
    already covers enrichment content.
    """
    _load_linker_index(catalog, documents, use_cache=use_cache)


def clear_schema_linker_cache() -> None:
    """Explicitly evict every cached _LinkerIndex — for tests, and for
    future rebuild invalidation, mirroring clear_canonical_scoring_cache()/
    clear_vector_index_cache()."""
    _index_cache.clear()


def schema_linker_cache_size() -> int:
    """Number of catalog fingerprints currently cached — exposed for tests."""
    return len(_index_cache)


# ── Ambiguity helper (shared by requirement-building and result-level
#    ambiguity detection) ──────────────────────────────────────────────────


def _materially_plausible_competitors(
    candidates: Sequence, family_key_by_fqn_lower: Mapping[str, str], min_confidence: float,
    *, confidence_of=lambda c: c.confidence, fqn_of=lambda c: c.object_fqn, type_of=lambda c: None,
) -> list:
    """
    The subset of `candidates` that are materially plausible alternatives
    to the single best one — within ANCHOR_AMBIGUITY_MARGIN, individually
    at least `min_confidence`, and NOT a same-family, same-object-type
    duplicate of the best one. Same-family suppression only fires when
    `type_of` also agrees (e.g. two TABLEs) — a TABLE and a VIEW built
    directly over it commonly land in the SAME canonical family (shared
    name tokens or a confirmed view-dependency edge) but serve genuinely
    different query shapes, so a real audit found suppressing the table
    just because its own view scored slightly higher silently dropped a
    required object with no genuine duplicate to blame. `type_of`
    defaults to a constant (every candidate compares equal), preserving
    the original "any same-family pair is a duplicate" behavior for
    callers that have no type information at all (e.g. RequirementCandidate,
    which is not object-type-specific).

    Never reports ambiguity "solely because many objects contain a common
    weak token": a candidate that is only weakly matched (low confidence)
    never clears `min_confidence` here even if it is technically present.
    """
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda c: (-confidence_of(c), fqn_of(c)))
    best = ordered[0]
    if confidence_of(best) < min_confidence:
        return []
    best_family = family_key_by_fqn_lower.get(fqn_of(best).lower())
    best_type = type_of(best)
    plausible = [best]
    for candidate in ordered[1:]:
        if confidence_of(best) - confidence_of(candidate) > ANCHOR_AMBIGUITY_MARGIN:
            continue
        if confidence_of(candidate) < min_confidence:
            continue
        candidate_family = family_key_by_fqn_lower.get(fqn_of(candidate).lower())
        same_family = best_family is not None and candidate_family is not None and best_family == candidate_family
        same_type = type_of(candidate) == best_type
        if same_family and same_type:
            continue
        plausible.append(candidate)
    return plausible


# ── Concept matching — Phase 6, Step 2B: aggregated into ONE
#    MatchedRequirement per (concept_kind, normalized_label) ─────────────


def _is_model_candidate(candidate: RequirementCandidate) -> bool:
    return candidate.match_kind == MATCH_KIND_MODEL_SEMANTIC


def _build_requirement(
    concept_kind: str, normalized_label: str, matched_terms: Sequence[str],
    candidates: Sequence[RequirementCandidate], *, family_key_by_fqn_lower: Mapping[str, str],
    min_ambiguity_confidence: float, max_candidates_per_requirement: int, strength: str = STRENGTH_SUPPORTING,
) -> MatchedRequirement:
    """
    SQL Architecture Correction, Phase 6, Step 4B1 — candidates are sorted
    in TWO STRICT TIERS: every non-model (structural/dictionary) candidate,
    by confidence descending, THEN every MATCH_KIND_MODEL_SEMANTIC
    candidate, by confidence descending — a model candidate is NEVER sorted
    ahead of a structural one regardless of raw confidence (Part 2:
    "dictionary-confirmed candidates always outrank otherwise-equivalent
    model-only candidates" / "structural/canonical spelling always wins").
    Since this tiering, `best_confidence` (and therefore `confidence` and
    `evidence` below) is UNCHANGED from before this step for any
    requirement that already had >=1 structural candidate — which is
    EVERY requirement a model candidate is ever added to (see
    _match_entities/_match_column_concepts, which only ever add model
    candidates to an ALREADY-EXISTING structural requirement group).

    `ambiguous` (Step 3C's existing semantics, untouched) is computed ONLY
    over the STRUCTURAL tier — model-only competition can never flip it
    (Part 4: "do not turn model-only competition into a new mandatory
    requirement"). `model_ambiguous` is a completely separate signal:
    True when 2+ MODEL-tier candidates are themselves materially plausible
    alternatives to EACH OTHER, regardless of whether a structural winner
    exists at all.
    """
    structural = [c for c in candidates if not _is_model_candidate(c)]
    model_only = [c for c in candidates if _is_model_candidate(c)]
    structural.sort(key=lambda c: (-c.confidence, c.object_fqn, c.column_name or ""))
    model_only.sort(key=lambda c: (-c.confidence, c.object_fqn, c.column_name or ""))
    ordered = structural + model_only
    candidate_total_count = len(ordered)
    bounded = tuple(ordered[:max_candidates_per_requirement])
    best_confidence = bounded[0].confidence if bounded else 0.0

    structural_bounded = tuple(c for c in bounded if not _is_model_candidate(c))
    model_bounded = tuple(c for c in bounded if _is_model_candidate(c))

    plausible = _materially_plausible_competitors(
        structural_bounded, family_key_by_fqn_lower, min_ambiguity_confidence,
        confidence_of=lambda c: c.confidence, fqn_of=lambda c: c.object_fqn,
    )
    # Step 3C, requirement 8: ambiguity is only meaningful for CORE (and
    # CONSTRAINT) concepts — a SUPPORTING/UNRESOLVED requirement's many weak
    # candidates must never be reported as "ambiguous" (that would just be
    # numerous weak supporting matches wearing an ambiguity label).
    ambiguous = len(plausible) > 1 and strength in (STRENGTH_CORE, STRENGTH_CONSTRAINT)

    model_plausible = _materially_plausible_competitors(
        model_bounded, family_key_by_fqn_lower, min_ambiguity_confidence,
        confidence_of=lambda c: c.confidence, fqn_of=lambda c: c.object_fqn,
    )
    model_ambiguous = len(model_plausible) > 1

    return MatchedRequirement(
        concept_kind=concept_kind,
        concept_key=_requirement_key(concept_kind, normalized_label),
        normalized_label=normalized_label,
        matched_terms=tuple(sorted(set(matched_terms))),
        candidates=bounded,
        candidate_total_count=candidate_total_count,
        confidence=best_confidence,
        ambiguous=ambiguous,
        evidence=bounded[0].evidence if bounded else (),
        strength=strength,
        model_ambiguous=model_ambiguous,
    )


# Match SCOPE — distinct from MATCH_KIND_*: how much of the question's own
# text one entity's winning match actually consumed. Ranked so a wider,
# more deliberate match (a whole business name, or a multi-word alias
# phrase actually found in the question) always wins over a match that is
# only one incidental shared token — Step 3C, requirement 1's own "longest
# phrase first" rule.
_ENTITY_MATCH_SCOPE_EXACT_NAME = 3
_ENTITY_MATCH_SCOPE_ALIAS_PHRASE = 2
_ENTITY_MATCH_SCOPE_ALIAS_TOKEN = 1

# "Sufficiently informative schema-document frequency" (requirement 2,
# bullet 2): a single-token alias match is CORE-eligible on its own only
# when this build's own entity_alias_token_normalized_idf says the token
# names a SMALL number of distinct entities' alias lists (never a
# hardcoded token list). Calibrated generically (never against a specific
# schema/question) against the real, generic-English-word distribution
# shape: a widely-shared column-name fragment every table's own vocabulary
# happens to include (e.g. "description", "name", "code", "amount" — every
# schema has these) clusters at roughly 0.30-0.60 normalized informativeness
# once the sharing entity count reaches even a few dozen out of a few
# hundred total entities, while a genuinely distinctive alias (naming only
# a handful of entities) clusters at 0.75+ — 0.65 sits cleanly in the gap
# between those two shapes.
_ENTITY_SINGLE_TOKEN_CORE_FLOOR = 0.65


def _classify_entity_strength(
    entity: BusinessEntity, winning_term: str, best_kind: str, scope: int, index: _LinkerIndex,
) -> str:
    """
    Step 3C, requirements 1/2/4 — deterministic, generic (never schema-
    specific) admission rule for one entity's preliminary strength, BEFORE
    the cross-entity substring-suppression pass (see _match_entities):

      1. A generic operation/contentless-noun word (requirement 4) never
         becomes CORE, regardless of match kind/scope — it is retained as
         SUPPORTING evidence only.
      2. An EXACT business-name match (the token/phrase IS this entity's
         own normalized_name) is always CORE — the strongest possible
         signal for THIS entity specifically, unaffected by how many OTHER
         entities happen to also use the same word incidentally elsewhere.
      3. A multi-word alias PHRASE actually found in the question is CORE —
         a deliberate multi-token overlap is vanishingly unlikely to be a
         coincidence.
      4. A single-token alias match needs real generic evidence: either the
         token is not widely shared as an incidental alias fragment across
         many DISTINCT entities (informativeness >= the configured floor),
         or — checked separately, after semantic/lexical ranks are known,
         see link_question_to_schema's own corroboration pass — independent
         lexical/semantic evidence corroborates this exact object. Absent
         that, a single-token match with genuinely competing candidates is
         UNRESOLVED (a plausible meaning we cannot confidently assign); a
         single-token match with only one candidate object is SUPPORTING.
    """
    if entity.normalized_name.lower() in _GENERIC_NON_ENTITY_TERMS:
        return STRENGTH_SUPPORTING
    if best_kind == MATCH_KIND_EXACT_BUSINESS_NAME:
        return STRENGTH_CORE
    if scope == _ENTITY_MATCH_SCOPE_ALIAS_PHRASE:
        return STRENGTH_CORE
    informativeness = index.entity_alias_token_normalized_idf.get(winning_term, 1.0)
    if informativeness >= _ENTITY_SINGLE_TOKEN_CORE_FLOOR:
        return STRENGTH_CORE
    return STRENGTH_UNRESOLVED if entity.ambiguous else STRENGTH_SUPPORTING


def _match_entities(
    raw_question_lower: str, question_tokens: Sequence[str], index: _LinkerIndex,
    *, family_key_by_fqn_lower: Mapping[str, str], min_ambiguity_confidence: float, max_candidates_per_requirement: int,
) -> tuple[list[MatchedRequirement], int, int]:
    """
    Entity concepts were already deduplicated by core.sqlservice.
    semantic_catalog (one BusinessEntity per normalized_name, with every
    plausible candidate object already attached) — this function's own job
    is only to decide WHICH entities the question mentions, and reshape
    each into a MatchedRequirement whose candidates carry their OWN
    per-object confidence directly from EntityCandidateObject.confidence
    (never a single flat value shared by every candidate — a richly
    documented table must score higher than a bare one sharing the same
    concept). Exact-name/phrase/synonym matches are never down-weighted by
    token document frequency (requirement 3: "exact business-name/entity
    phrase matches remain strong").

    Step 3C additionally classifies each entity's `strength` (see
    _classify_entity_strength) and suppresses a single-token match that is
    a redundant SUBSTRING of a stronger, already-CORE multi-word phrase
    matched for a DIFFERENT entity (requirement 1: "suppress redundant
    substring requirements already represented by a stronger phrase" — the
    weaker match is demoted to SUPPORTING, never dropped: its evidence is
    preserved). Returns (requirements, substring_suppressed_count,
    weak_single_token_demoted_count) for SchemaLinkingResult's own
    observability (requirement 9).
    """
    question_token_set = set(question_tokens)
    # matches_by_entity_key: normalized_name.lower() -> [entity, terms, best_kind, scope, winning_term]
    matches_by_entity_key: dict[str, list] = {}

    def _consider(entity: BusinessEntity, term: str, match_kind: str, scope: int) -> None:
        key = entity.normalized_name.lower()
        existing = matches_by_entity_key.get(key)
        if existing is None:
            matches_by_entity_key[key] = [entity, [term], match_kind, scope, term]
            return
        rec = existing
        if term not in rec[1]:
            rec[1].append(term)
        if scope > rec[3]:
            rec[2], rec[3], rec[4] = match_kind, scope, term

    for token in sorted(question_token_set):
        for entry in index.entity_exact_token_index.get(token, ()):
            _consider(entry.entity, token, MATCH_KIND_EXACT_BUSINESS_NAME, _ENTITY_MATCH_SCOPE_EXACT_NAME)
    for phrase in sorted(index.entity_alias_phrase_index):
        if phrase in raw_question_lower:
            for entry in index.entity_alias_phrase_index[phrase]:
                _consider(entry.entity, phrase, MATCH_KIND_SYNONYM, _ENTITY_MATCH_SCOPE_ALIAS_PHRASE)
    for token in sorted(question_token_set):
        for entry in index.entity_alias_token_index.get(token, ()):
            _consider(entry.entity, token, MATCH_KIND_SYNONYM, _ENTITY_MATCH_SCOPE_ALIAS_TOKEN)

    # Preliminary strength per entity, plus the set of question tokens
    # "consumed" by some OTHER, STRONGER entity match — either a CORE
    # multi-word phrase (the longest-phrase-first rule's own evidence,
    # requirement 1) or a CORE exact-business-name match. The latter case
    # is the same principle one level up: a token that IS one entity's own
    # primary identity (e.g. "student") routinely also turns up as an
    # INCIDENTAL borrowed alias on a handful of unrelated entities (e.g. a
    # table that merely happens to have a "student"-prefixed name of its
    # own) — a small alias-sharing count alone cannot tell those two
    # situations apart (both look "rare"), so once the token has already
    # won as some entity's OWN exact name, every OTHER entity's single-
    # token alias match on that exact same token is redundant, never
    # independently CORE.
    prelim: dict[str, tuple] = {}
    strong_consumed_tokens: set[str] = set()
    for key, (entity, terms, best_kind, scope, winning_term) in matches_by_entity_key.items():
        strength = _classify_entity_strength(entity, winning_term, best_kind, scope, index)
        prelim[key] = (entity, terms, best_kind, scope, winning_term, strength)
        if strength == STRENGTH_CORE and scope == _ENTITY_MATCH_SCOPE_ALIAS_PHRASE:
            strong_consumed_tokens.update(winning_term.split())
        elif strength == STRENGTH_CORE and scope == _ENTITY_MATCH_SCOPE_EXACT_NAME:
            strong_consumed_tokens.add(winning_term)

    requirements: list[MatchedRequirement] = []
    substring_suppressed_count = 0
    weak_single_token_demoted_count = 0
    for key in sorted(prelim):
        entity, terms, best_kind, scope, winning_term, strength = prelim[key]
        if (
            scope == _ENTITY_MATCH_SCOPE_ALIAS_TOKEN and strength == STRENGTH_CORE
            and winning_term in strong_consumed_tokens
        ):
            strength = STRENGTH_SUPPORTING
            substring_suppressed_count += 1
        elif scope == _ENTITY_MATCH_SCOPE_ALIAS_TOKEN and strength in (STRENGTH_SUPPORTING, STRENGTH_UNRESOLVED):
            weak_single_token_demoted_count += 1

        candidates = [
            RequirementCandidate(
                object_fqn=c.object_fqn, column_name=None, technical_label=entity.normalized_name,
                match_kind=best_kind, confidence=c.confidence, raw_confidence=c.confidence, informativeness=1.0,
                evidence=(EvidenceItem(kind=best_kind, detail=entity.normalized_name),) + c.evidence[:2],
            )
            for c in entity.candidate_objects
        ]
        # SQL Architecture Correction, Phase 6, Step 4B1, Part 2 — validated
        # model-inferred business_name/alias/entity_concept evidence may add
        # ADDITIONAL candidate objects underneath this ALREADY-MATCHED
        # entity requirement — never a new requirement (this lookup is keyed
        # by the entity's OWN normalized_name, already established above by
        # real structural/dictionary matching). An object already present
        # among `entity.candidate_objects` is never duplicated here — a
        # real structural candidate for this exact object always already
        # outranks anything this loop could add for it.
        structural_object_fqns_lower = {c.object_fqn.lower() for c in entity.candidate_objects}
        for model_entry in index.model_entity_alias_index.get(entity.normalized_name.lower(), ()):
            if model_entry.object_fqn.lower() in structural_object_fqns_lower:
                continue
            candidates.append(
                RequirementCandidate(
                    object_fqn=model_entry.object_fqn, column_name=None, technical_label=entity.normalized_name,
                    match_kind=MATCH_KIND_MODEL_SEMANTIC, confidence=model_entry.confidence,
                    raw_confidence=model_entry.confidence, informativeness=model_entry.informativeness,
                    evidence=model_entry.evidence,
                )
            )
        requirements.append(
            _build_requirement(
                CONCEPT_KIND_ENTITY, entity.normalized_name, terms, candidates,
                family_key_by_fqn_lower=family_key_by_fqn_lower, min_ambiguity_confidence=min_ambiguity_confidence,
                max_candidates_per_requirement=max_candidates_per_requirement, strength=strength,
            )
        )
    return requirements, substring_suppressed_count, weak_single_token_demoted_count


# Reused verbatim from core.sqlservice.semantic_catalog — matching a
# question token against the SAME category vocabulary that already decided
# a column's normalized_measure/dimension_kind/date role is a decisive
# confidence boost (MATCH_KIND_CATEGORY_KEYWORD), never a second, possibly-
# conflicting keyword table.
_MEASURE_CATEGORY_BY_KEYWORD: Mapping[str, str] = MappingProxyType({
    keyword: category for category, keywords in _MEASURE_KEYWORD_CATEGORIES for keyword in keywords
})
_DIMENSION_KIND_BY_KEYWORD: Mapping[str, str] = MappingProxyType({
    keyword: kind for kind, keywords in _DIMENSION_TOKEN_CATEGORIES for keyword in keywords
})
_DATE_ROLE_BY_KEYWORD: Mapping[str, str] = MappingProxyType({
    keyword: role for role, keywords in _DATE_ROLE_KEYWORDS for keyword in keywords
})
_CATEGORY_BY_KIND: Mapping[str, Mapping[str, str]] = MappingProxyType({
    CONCEPT_KIND_MEASURE: _MEASURE_CATEGORY_BY_KEYWORD,
    CONCEPT_KIND_DIMENSION: _DIMENSION_KIND_BY_KEYWORD,
    CONCEPT_KIND_DATE_ROLE: _DATE_ROLE_BY_KEYWORD,
})


# Step 3C, requirement 4 — operation-aware CORE promotion for column-
# derived (measure/dimension/date-role) concepts: an aggregation operation
# genuinely REQUIRES a measure target, a grouped-aggregation genuinely
# REQUIRES a grouping dimension, and a trend genuinely REQUIRES a date
# role — these three cases are the ONLY way a column-derived concept
# becomes CORE. Generic operation vocabulary only (see OPERATION_* in this
# module's own docstring) — never a schema-specific rule.
_AGGREGATION_OPERATIONS = frozenset({OPERATION_SUM, OPERATION_AVERAGE, OPERATION_MINIMUM, OPERATION_MAXIMUM})


def _classify_column_concept_strength(
    concept_kind: str, group_is_category_hit: bool, operation: str, wants_grouping: bool, wants_trend: bool,
) -> str:
    """
    Step 3C, requirements 3/4: "a column-name match alone must not
    automatically create a CORE business entity" — a measure/dimension/
    date-role concept is CORE only when the question's own detected
    operation genuinely targets that concept kind; it is CONSTRAINT when an
    explicit category-keyword (e.g. a status/date-role English word the
    question actually used — MATCH_KIND_CATEGORY_KEYWORD) was matched
    without being the operation's own target; otherwise it is SUPPORTING
    (a bare column-name-fragment overlap). An IDENTIFIER concept is always
    SUPPORTING here — core.sqlservice.join_planner maps it to JOIN_HINT
    regardless of strength (identifiers are never independently mandatory
    coverage, Step 3B/3C alike).
    """
    if concept_kind == CONCEPT_KIND_IDENTIFIER:
        return STRENGTH_SUPPORTING
    if concept_kind == CONCEPT_KIND_MEASURE:
        if operation in _AGGREGATION_OPERATIONS:
            return STRENGTH_CORE
        return STRENGTH_CONSTRAINT if group_is_category_hit else STRENGTH_SUPPORTING
    if concept_kind == CONCEPT_KIND_DIMENSION:
        if operation == OPERATION_GROUPED_AGGREGATION or wants_grouping:
            return STRENGTH_CORE
        return STRENGTH_CONSTRAINT if group_is_category_hit else STRENGTH_SUPPORTING
    if concept_kind == CONCEPT_KIND_DATE_ROLE:
        if operation == OPERATION_TREND or wants_trend:
            return STRENGTH_CORE
        return STRENGTH_CONSTRAINT if group_is_category_hit else STRENGTH_SUPPORTING
    return STRENGTH_SUPPORTING


def _match_column_concepts(
    question_tokens: Sequence[str], index: _LinkerIndex,
    *, family_key_by_fqn_lower: Mapping[str, str], min_ambiguity_confidence: float,
    max_candidates_per_requirement: int, informativeness_floor: float,
    operation: str = OPERATION_UNKNOWN, wants_grouping: bool = False, wants_trend: bool = False,
) -> tuple[list[MatchedRequirement], int, int]:
    """
    Aggregates every (token, catalog entry) hit into MatchedRequirement
    groups keyed by (concept_kind, token) — Phase 6, Step 2B's core fix:
    the SAME token matching the SAME concept kind across many different
    objects/columns (e.g. "class" matching a "ClassID" identifier on 50
    tables) becomes ONE requirement with 50 candidates, never 50
    independent requirements. A DIFFERENT token mapping to the same
    underlying technical category (e.g. "revenue" and "sales" both
    categorizing as the "amount" measure) deliberately stays a SEPARATE
    requirement — the user said two different words, each is its own
    potential information need (requirement 6: "do not collapse genuinely
    different meanings").

    Each candidate's confidence is scaled by this build's own bounded,
    floored token informativeness (requirement 3) — a token shared by many
    distinct objects contributes less per candidate, without ever
    excluding it outright.

    Returns (requirements, raw_match_count, raw_candidate_count) —
    `raw_match_count` counts every (token, entry) pair scanned;
    `raw_candidate_count` counts the deduplicated (object, column) hits
    actually kept per requirement group (before the max_candidates_per_
    requirement bound) — the difference is Step 2B's own "duplicate
    matches merged" observability metric.
    """
    grouped: dict[tuple[str, str], dict[tuple[str, str], RequirementCandidate]] = defaultdict(dict)
    matched_terms_by_group: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_is_category_hit: dict[tuple[str, str], bool] = defaultdict(bool)
    raw_match_count = 0

    for token in sorted(set(question_tokens)):
        for entry in index.column_token_index.get(token, ()):
            raw_match_count += 1
            if entry.object_fqn.lower() not in index.eligible_fqns_lower:
                continue
            category_map = _CATEGORY_BY_KIND.get(entry.concept_kind)
            is_category_hit = bool(category_map and category_map.get(token) == entry.label)
            match_kind = MATCH_KIND_CATEGORY_KEYWORD if is_category_hit else MATCH_KIND_COLUMN_TOKEN
            informativeness = _token_informativeness(index, token, informativeness_floor)
            effective_confidence = _clip01(entry.base_confidence * informativeness)

            group_key = (entry.concept_kind, token)
            if is_category_hit:
                group_is_category_hit[group_key] = True
            # Dedup key includes the technical label so genuinely distinct
            # catalog interpretations of the SAME (object, column) are
            # never collapsed into one — e.g. a column that is both a
            # PRIMARY_KEY and a FOREIGN_KEY (two real IdentifierConcept
            # rows), or a date column with two plausible role
            # interpretations (e.g. "started" and "completed") — both must
            # survive as separate candidates so ambiguity detection can
            # still see the distinction.
            candidate_key = (entry.object_fqn, entry.column_name, entry.label)
            matched_terms_by_group[group_key].add(token)

            existing = grouped[group_key].get(candidate_key)
            if existing is not None and existing.confidence >= effective_confidence:
                continue
            grouped[group_key][candidate_key] = RequirementCandidate(
                object_fqn=entry.object_fqn, column_name=entry.column_name, technical_label=entry.label,
                match_kind=match_kind, confidence=effective_confidence, raw_confidence=entry.base_confidence,
                informativeness=informativeness,
                evidence=(EvidenceItem(kind=MATCH_KIND_COLUMN_TOKEN, detail=token),) + entry.evidence[:2],
            )

    requirements: list[MatchedRequirement] = []
    raw_candidate_count = 0
    for group_key in sorted(grouped):
        concept_kind, token = group_key
        candidates = list(grouped[group_key].values())
        raw_candidate_count += len(candidates)

        # SQL Architecture Correction, Phase 6, Step 4B1, Part 3 —
        # validated model-inferred measure/dimension/date-role/identifier-
        # role evidence may add ADDITIONAL column candidates underneath
        # THIS ALREADY-EXISTING (concept_kind, token) group — never a new
        # group (this loop only ever runs over `grouped` keys real
        # structural matching already produced). An (object, column, label)
        # triple already present structurally is never duplicated here —
        # real structural evidence for that exact interpretation already
        # outranks anything model evidence could add for it.
        existing_candidate_keys = set(grouped[group_key].keys())
        for model_entry in index.model_column_token_index.get(token, ()):
            if model_entry.concept_kind != concept_kind:
                continue
            model_key = (model_entry.object_fqn, model_entry.column_name, model_entry.label)
            if model_key in existing_candidate_keys:
                continue
            existing_candidate_keys.add(model_key)
            # Phase 6, Step 6G, Part 3 — the SAME per-build token
            # informativeness floor that already discounts a structural
            # column-token match (see `informativeness` a few lines above,
            # same `token`/`informativeness_floor`) applies identically to
            # a model-inferred one: a role label/column-name token many
            # DISTINCT real objects already share is generic regardless of
            # whether a human dictionary or a model claimed it.
            model_informativeness = _token_informativeness(index, token, informativeness_floor)
            model_effective_confidence = _clip01(model_entry.confidence * model_informativeness)
            candidates.append(
                RequirementCandidate(
                    object_fqn=model_entry.object_fqn, column_name=model_entry.column_name,
                    technical_label=model_entry.label, match_kind=MATCH_KIND_MODEL_SEMANTIC,
                    confidence=model_effective_confidence, raw_confidence=model_entry.confidence,
                    informativeness=model_informativeness, evidence=model_entry.evidence,
                )
            )

        strength = _classify_column_concept_strength(
            concept_kind, group_is_category_hit.get(group_key, False), operation, wants_grouping, wants_trend,
        )
        requirements.append(
            _build_requirement(
                concept_kind, token, sorted(matched_terms_by_group[group_key]), candidates,
                family_key_by_fqn_lower=family_key_by_fqn_lower, min_ambiguity_confidence=min_ambiguity_confidence,
                max_candidates_per_requirement=max_candidates_per_requirement, strength=strength,
            )
        )
    return requirements, raw_match_count, raw_candidate_count


# ── Anchor scoring ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ScoredCandidate:
    object_fqn: str
    object_type: str
    retrieval_tier: int
    supporting_concept_refs: tuple[str, ...]
    supporting_columns: tuple[str, ...]
    semantic_score: float | None
    lexical_score: float | None
    catalog_score: float
    example_score: float
    canonical_score: float
    model_semantic_score: float
    combined_score: float
    evidence: tuple[EvidenceItem, ...]
    is_admitted: bool
    admission_reasons: tuple[str, ...]


def _admission_reasons(
    *, catalog_score: float, semantic_score: float | None, lexical_rank: int | None, example_score: float,
    model_semantic_score: float, min_semantic_score: float, max_lexical_admission_rank: int,
) -> tuple[str, ...]:
    """
    The exact question-relevance signal(s) that qualify this object for
    consideration — Phase 6, Step 2A's core rule: "canonical score alone
    is never an admission signal". `catalog_score`/`example_score` are
    already the noisy-OR of matched-requirement-candidate confidences /
    QUALIFIED example similarities (values below their own minimum never
    entered that combination at all — see link_question_to_schema's own
    example-filtering) — so catalog_score > 0 / example_score > 0 already
    mean "at least one real match/compatible example cleared its bar".
    Semantic uses the RAW cosine score (bounded [0,1], meaningful across
    corpora) against its own configured minimum. Lexical uses RANK, not
    raw score — core.sqlservice.lexical_index's BM25F score is field-
    weighted and unbounded, so only "is this object among the question's
    own top-K lexical matches" stays meaningful across different corpora/
    builds (see SQL_LINKING_MAX_LEXICAL_ADMISSION_RANK's own config
    docstring).
    """
    reasons: list[str] = []
    if catalog_score > 0.0:
        reasons.append(ADMISSION_REASON_CONCEPT_MATCH)
    if lexical_rank is not None and lexical_rank <= max_lexical_admission_rank:
        reasons.append(ADMISSION_REASON_LEXICAL_MATCH)
    if semantic_score is not None and semantic_score >= min_semantic_score:
        reasons.append(ADMISSION_REASON_SEMANTIC_MATCH)
    if example_score > 0.0:
        reasons.append(ADMISSION_REASON_EXAMPLE_MATCH)
    # SQL Architecture Correction, Phase 6, Step 4B1, Part 6 — "model
    # evidence may make an object available as a terminal candidate": an
    # object with ZERO structural/dictionary catalog support (catalog_score
    # == 0.0) can still be admitted purely via validated model evidence.
    # Deliberately its own, separate reason — never folded into
    # ADMISSION_REASON_CONCEPT_MATCH, which remains structural-only.
    if model_semantic_score > 0.0:
        reasons.append(ADMISSION_REASON_MODEL_MATCH)
    return tuple(reasons)


def _score_all_eligible_objects(
    index: _LinkerIndex,
    matched_requirements: Sequence[MatchedRequirement],
    semantic_rank_by_fqn_lower: Mapping[str, int],
    semantic_score_by_fqn_lower: Mapping[str, float],
    lexical_rank_by_fqn_lower: Mapping[str, int],
    lexical_score_by_fqn_lower: Mapping[str, float],
    example_confidences_by_fqn_lower: Mapping[str, list[float]],
    *,
    max_evidence_items_per_anchor: int,
    min_semantic_score: float,
    max_lexical_admission_rank: int,
) -> list[_ScoredCandidate]:
    # Per-object attribution: which requirement(s) does this object have a
    # candidate for, and at what (already informativeness-adjusted)
    # confidence — Phase 6, Step 2B: keyed by the NORMALIZED requirement
    # key, never a per-column ref, so an object with candidates for the
    # SAME requirement via multiple columns still contributes it only once
    # to `supporting_concept_refs`.
    candidates_by_fqn_lower: dict[str, list[tuple[str, float, RequirementCandidate]]] = defaultdict(list)
    for requirement in matched_requirements:
        for candidate in requirement.candidates:
            candidates_by_fqn_lower[candidate.object_fqn.lower()].append(
                (requirement.concept_key, candidate.confidence, candidate)
            )

    scored: list[_ScoredCandidate] = []
    for fqn_lower in index.eligible_fqns_lower:
        own = candidates_by_fqn_lower.get(fqn_lower, [])
        # Phase 6, Step 4B1, Part 5 — `catalog_score` stays STRUCTURAL/
        # dictionary-only (byte-identical to before this step); model-tier
        # candidate confidences are combined into their OWN separate
        # `model_semantic_score` term instead of being folded in here, so
        # the two evidence sources are always independently observable and
        # never conflated.
        catalog_score = _noisy_or([confidence for _ref, confidence, cand in own if not _is_model_candidate(cand)])
        model_semantic_score = _noisy_or([confidence for _ref, confidence, cand in own if _is_model_candidate(cand)])
        # example_confidences_by_fqn_lower already contains ONLY
        # similarities that cleared min_example_similarity (filtered by the
        # caller before this function ever sees them) — example_score > 0
        # therefore already means "at least one compatible example cleared
        # its own configured minimum", never a sub-threshold example
        # sneaking in via noisy-OR.
        example_score = _noisy_or(example_confidences_by_fqn_lower.get(fqn_lower, []))
        canonical_score = index.canonical_score_by_fqn_lower.get(fqn_lower, 0.0)
        semantic_rank = semantic_rank_by_fqn_lower.get(fqn_lower)
        lexical_rank = lexical_rank_by_fqn_lower.get(fqn_lower)
        semantic_raw = semantic_score_by_fqn_lower.get(fqn_lower)
        lexical_raw = lexical_score_by_fqn_lower.get(fqn_lower)
        semantic_term = _rank_to_bounded_score(semantic_rank)
        lexical_term = _rank_to_bounded_score(lexical_rank)

        combined = (
            ANCHOR_WEIGHT_CATALOG * catalog_score
            + ANCHOR_WEIGHT_SEMANTIC * semantic_term
            + ANCHOR_WEIGHT_LEXICAL * lexical_term
            + ANCHOR_WEIGHT_CANONICAL * canonical_score
            + ANCHOR_WEIGHT_EXAMPLE * example_score
            + ANCHOR_WEIGHT_MODEL_SEMANTIC * model_semantic_score
        )

        admission_reasons = _admission_reasons(
            catalog_score=catalog_score, semantic_score=semantic_raw, lexical_rank=lexical_rank,
            example_score=example_score, model_semantic_score=model_semantic_score,
            min_semantic_score=min_semantic_score, max_lexical_admission_rank=max_lexical_admission_rank,
        )

        # Every eligible object is scored and included here, even at
        # combined_score == 0.0 / with no admission reason at all —
        # requirement 2's "complete-schema linking": the full eligible set
        # must be considered before ANY output bound is applied. Whether an
        # object may become an ANCHOR is decided entirely by `is_admitted`
        # downstream (see _select_anchors) — canonical_score contributes to
        # `combined` (ranking) but never to `admission_reasons` (Step 2A's
        # core rule).
        evidence: list[EvidenceItem] = []
        for _ref, _confidence, candidate in sorted(own, key=lambda item: (-item[1], item[0]))[:max_evidence_items_per_anchor]:
            evidence.extend(candidate.evidence[:1])
        if semantic_rank is not None:
            evidence.append(EvidenceItem(kind="SEMANTIC_RANK", detail=str(semantic_rank)))
        if lexical_rank is not None:
            evidence.append(EvidenceItem(kind="LEXICAL_RANK", detail=str(lexical_rank)))
        if canonical_score > 0.0:
            evidence.append(EvidenceItem(kind="CANONICAL_SCORE", detail=f"{canonical_score:.4f}"))
        evidence = evidence[:max_evidence_items_per_anchor]

        own_columns = sorted({c.column_name for _r, _c, c in own if c.column_name})

        scored.append(
            _ScoredCandidate(
                object_fqn=index.fqn_by_lower[fqn_lower],
                object_type=index.object_type_by_fqn_lower[fqn_lower],
                retrieval_tier=index.tier_by_fqn_lower[fqn_lower],
                supporting_concept_refs=tuple(sorted({ref for ref, _c, _cand in own})),
                supporting_columns=tuple(own_columns),
                semantic_score=semantic_raw,
                lexical_score=lexical_raw,
                catalog_score=catalog_score,
                example_score=example_score,
                canonical_score=canonical_score,
                model_semantic_score=model_semantic_score,
                combined_score=_clip01(combined),
                evidence=tuple(evidence),
                is_admitted=bool(admission_reasons),
                admission_reasons=admission_reasons,
            )
        )

    return scored


# ── Relevance-first anchor selection (deterministic) ─────────────────────


def _structural_component_score(candidate: _ScoredCandidate) -> float:
    """`combined_score` with the model-semantic contribution subtracted
    back out — Phase 6, Step 6G, Part 3's "structural evidence outranks
    model evidence" tiebreaker. Two candidates that happen to reach the
    same overall `combined_score` are never equally trustworthy when one
    got there mostly through validated-but-advisory model inference and
    the other through real catalog/lexical/semantic/example evidence —
    this makes that distinction visible as its own sortable quantity
    without recomputing anything `_score_all_eligible_objects` did not
    already compute."""
    return _clip01(candidate.combined_score - ANCHOR_WEIGHT_MODEL_SEMANTIC * candidate.model_semantic_score)


def _best_requirement_confidence(
    candidate: _ScoredCandidate, requirement_confidence_by_concept_and_fqn: Mapping[tuple[str, str], float],
) -> float:
    """The candidate's own strongest per-CONCEPT requirement confidence,
    across every concept it supports — Phase 6, Step 6G, Part 3's most
    direct available proxy for "how strongly does this specific candidate
    satisfy the specific mandatory concept it is meant to cover", as
    distinct from its overall anchor `combined_score` (which additionally
    blends in corpus-wide semantic/lexical RANK terms describing the
    object's relevance to the question AS A WHOLE, not to any one
    concept). A candidate whose only real strength is "generally well-
    ranked for this question" must never out-prioritize, for a SPECIFIC
    concept, an object that is that concept's own clear best match."""
    fqn_lower = candidate.object_fqn.lower()
    values = [
        requirement_confidence_by_concept_and_fqn.get((ref, fqn_lower), 0.0)
        for ref in candidate.supporting_concept_refs
    ]
    return max(values) if values else 0.0


# ── Generic supporting-anchor eligibility (Phase 6, Step 6I, Part 2) ──────
#
# A SUPPORTING-strength requirement (this module's own weakest concept-
# strength tier — see STRENGTH_SUPPORTING's own docstring) is never counted
# toward `required_concept_refs` (see link_question_to_schema's own
# construction of it), so it can otherwise only ever reach an anchor slot
# through _select_anchors' small, generic Phase 2 fill (bounded by
# max_non_concept_anchor_fill), competing purely on overall combined_score
# against every other non-concept-matched candidate — regardless of how
# directly and specifically its own evidence supports the concept it
# actually matched. When that evidence is strong enough that treating it as
# "no real concept support at all" is itself the wrong call, this promotes
# ONLY that one (concept, object) candidate's own concept_key into the SAME
# mandatory-coverage pool CORE/CONSTRAINT requirements already use — never
# the requirement's `strength` itself (which stays SUPPORTING;
# core.sqlservice.join_planner's own STRENGTH_SUPPORTING ->
# REQUIREMENT_ROLE_JOIN_HINT mapping, and therefore "never counted as plan
# coverage", is completely unaffected by this promotion — a qualifying
# candidate may seed a plan but a plan still needs real confirmed
# relationships to use it for anything).
#
# Every threshold below REUSES an already-documented, already-battle-tested
# boundary from elsewhere in this same module — never a new value derived
# against any specific schema/question (Part 2: "do not derive a new
# threshold from golden cases"):
#   - MODEL_CANDIDATE_CONFIDENCE_CAP (0.50) is the documented ceiling every
#     MATCH_KIND_MODEL_SEMANTIC candidate is capped at — requiring a real,
#     structural/dictionary candidate's OWN (already informativeness-
#     adjusted) confidence to meet or exceed that same ceiling is exactly
#     "stronger than the strongest possible model-only signal", a
#     principled, already-existing bound to reuse for "is this real
#     evidence strong enough to trust as if it were mandatory".
#   - _ENTITY_SINGLE_TOKEN_CORE_FLOOR (0.65) is the SAME bounded IDF-like
#     informativeness floor _classify_entity_strength already uses to
#     decide whether a single-token alias match is specific enough to be
#     CORE on its own — reused verbatim (never a second, conflicting
#     informativeness bound) for every concept kind, not entities alone.
_SUPPORTING_ANCHOR_PROMOTION_CONFIDENCE_FLOOR = MODEL_CANDIDATE_CONFIDENCE_CAP
_SUPPORTING_ANCHOR_PROMOTION_INFORMATIVENESS_FLOOR = _ENTITY_SINGLE_TOKEN_CORE_FLOOR


def _qualifying_supporting_concept_refs(
    requirements: Sequence[MatchedRequirement], admitted_fqns_lower: frozenset[str], eligible_fqns_lower: frozenset[str],
) -> frozenset[str]:
    """
    Phase 6, Step 6I, Part 2 — the concept_key of every SUPPORTING
    requirement that has AT LEAST ONE candidate meeting every eligibility
    criterion below, ALL of them required at once (never "any one of"):
    real structural/dictionary evidence (never MATCH_KIND_MODEL_SEMANTIC —
    "not model-semantic or authority-only"; core.sqlservice.view_authority's
    own MATCH_KIND_VIEW_AUTHORITY candidates are never present here at all,
    since core.sqlservice.schema_plan_options only augments linking with
    view authority AFTER this function has already returned), strong
    confidence (>= _SUPPORTING_ANCHOR_PROMOTION_CONFIDENCE_FLOOR), strong
    informativeness (>= _SUPPORTING_ANCHOR_PROMOTION_INFORMATIVENESS_FLOOR —
    "a generic shared word must not create an anchor"), already present in
    the bounded relevance pool (`admitted_fqns_lower`), and a real eligible
    (Tier 1/2) object (`eligible_fqns_lower` — defense in depth; every
    candidate here should already satisfy this by construction).

    Never promotes a whole requirement merely because ONE weak candidate
    among several happens to exist for it — every individual candidate is
    checked on its own merits, and only the requirement's concept_key (never
    a specific object) is promoted, exactly mirroring how CORE/CONSTRAINT
    requirements already work: `_select_anchors` still picks WHICH admitted
    candidate best satisfies a promoted concept, using its own existing,
    unmodified greedy-strength ordering. Never touches STRENGTH_CORE/
    CONSTRAINT/UNRESOLVED requirements at all (Part 2 is scoped to
    SUPPORTING only).
    """
    promoted: set[str] = set()
    for requirement in requirements:
        if requirement.strength != STRENGTH_SUPPORTING:
            continue
        for candidate in requirement.candidates:
            if candidate.match_kind == MATCH_KIND_MODEL_SEMANTIC:
                continue
            if candidate.confidence < _SUPPORTING_ANCHOR_PROMOTION_CONFIDENCE_FLOOR:
                continue
            if candidate.informativeness < _SUPPORTING_ANCHOR_PROMOTION_INFORMATIVENESS_FLOOR:
                continue
            fqn_lower = candidate.object_fqn.lower()
            if fqn_lower not in eligible_fqns_lower or fqn_lower not in admitted_fqns_lower:
                continue
            promoted.add(requirement.concept_key)
            break
    return frozenset(promoted)


def _select_anchors(
    admitted: Sequence[_ScoredCandidate], required_concept_refs: frozenset[str], max_anchors: int,
    max_non_concept_fill: int = SQL_LINKING_MAX_NON_CONCEPT_ANCHOR_FILL,
    *,
    requirement_confidence_by_concept_and_fqn: Mapping[tuple[str, str], float] = MappingProxyType({}),
) -> tuple[list[_ScoredCandidate], bool]:
    """
    Phase 6, Step 2A/2B, revised Step 6G Part 3: operates ONLY over
    `admitted` (already relevance-qualified — see _admission_reasons)
    candidates, never the complete eligible set, and `required_concept_
    refs` are NORMALIZED requirement keys (Step 2B) — one per distinct
    business concept, never one per (object, column).

    Phase 1 (relevance-first coverage): repeatedly picks the single
    ADMITTED candidate that is the STRONGEST available match, ranked by
    (1) `_best_requirement_confidence` — this candidate's OWN best per-
    CONCEPT requirement confidence, the most direct available signal of
    "how strongly does this specific candidate satisfy the specific
    mandatory concept it is meant to cover" (added after an earlier
    revision of this same fix used only the object's overall
    `combined_score` here and thereby still let a candidate whose
    OVERALL relevance happened to be higher — via corpus-wide semantic/
    lexical RANK terms unrelated to this concept — grab a concept a
    DIFFERENT, more specifically-evidenced candidate was actually the
    clear best match for); (2) `combined_score` descending (the object's
    own overall relevance, still a real secondary signal); (3)
    `_structural_component_score` — real evidence over model-only; (4)
    how many currently-uncovered required concepts it happens to support;
    (5) object_fqn. If the top-ranked candidate covers at least one
    currently-uncovered required concept,
    selects it; otherwise the candidate is discarded (it can never become
    useful later — `covered` only grows) and the search continues down
    the SAME strength-ordered list. This replaces Step 2B's original
    "always prefer whichever candidate covers the most uncovered concepts,
    breaking ties by score" rule, which let a candidate whose validated
    model-inferred business_domain/alias/status/operation evidence happens
    to nominally touch MANY distinct requirements at once (exactly what a
    denormalizing business view legitimately does) win an anchor slot
    ahead of a single strongly, directly relevant object — Phase 6, Step
    6F's own offline evaluation proved this in production: enabling real
    view semantics monotonically REDUCED required-object coverage (most
    -> fewer -> fewest) purely from this ordering, with zero change to the
    requirements or underlying schema. Under the new order, breadth can
    still matter (a candidate covering three concepts at combined_score
    0.6 each beats three separate single-concept candidates at 0.4 each
    only insofar as it's individually the strongest available choice for
    each), but breadth alone, at a WEAKER score, can never jump the queue
    — "one strong match must be able to beat several weak generic
    matches" (Part 3) applied literally to this loop's own ordering.
    Every concept a selected candidate additionally happens to cover is
    still credited (`covered` still grows by the FULL new_coverage set
    per pick, exactly as before) — this only changes WHICH candidate is
    picked at each step, never how much credit a pick earns. A
    requirement is considered COVERED the moment ONE strong candidate is
    selected for it (requirement 4) — the other 49 tables sharing a
    common column are never independently required.

    Phase 2 (non-concept admitted fill): admitted candidates with NO
    concept support at all (pure lexical/semantic/example matches) are
    added next, ranked by combined_score, bounded by `max_non_concept_fill`
    EXTRA anchors beyond Phase 1's own selection (Step 3E, Part 5) — never
    a blind pad all the way to `max_anchors`. Real vector/lexical evidence
    routinely admits dozens of tangentially-related objects via SEMANTIC_
    MATCH/LEXICAL_MATCH rank cutoffs alone; without this second, smaller
    bound, Phase 2 would happily fill every remaining slot up to the
    ceiling regardless of true relevance ("full evidence must not
    automatically return max_anchor_candidates anchors"). `max_anchors`
    remains the hard ceiling either way (still enforced by the caller after
    ambiguity-alternative preservation). Concept-supported admitted
    candidates that were NOT selected in Phase 1 are deliberately NOT
    re-added here — see _preserve_ambiguous_concept_candidates for the one
    legitimate exception (a genuinely close competitor for an already-
    covered requirement), called separately by the caller.

    Returns (selected_in_priority_order, coverage_incomplete).
    """
    by_fqn = {c.object_fqn: c for c in admitted}
    # Step 3E, Part 5: a candidate only belongs to the MANDATORY coverage
    # pool when it supports at least one concept that is actually required
    # (CORE/CONSTRAINT — see required_concept_refs' own construction at the
    # call site). A candidate whose ONLY concept support is a SUPPORTING/
    # UNRESOLVED requirement is functionally the same as "no concept
    # support" for anchor-selection purposes — it must never force its own
    # dedicated anchor slot in Phase 1, but it remains fully eligible for
    # Phase 2's bounded evidence-ranked fill (independent semantic/lexical/
    # example evidence may still surface it, just never as mandatory
    # coverage).
    concept_supported = [c for c in admitted if set(c.supporting_concept_refs) & required_concept_refs]
    non_concept = [c for c in admitted if not (set(c.supporting_concept_refs) & required_concept_refs)]

    covered: set[str] = set()
    selected: list[str] = []
    # Fixed, strength-first order — computed ONCE (never re-sorted by a
    # `covered`-dependent breadth term as the primary key), so a candidate's
    # queue POSITION never changes as coverage accumulates; only whether it
    # still has anything new left to contribute does.
    pool = sorted(
        concept_supported,
        key=lambda c: (
            -_best_requirement_confidence(c, requirement_confidence_by_concept_and_fqn),
            -c.combined_score,
            -_structural_component_score(c),
            -len(c.supporting_concept_refs),
            c.object_fqn,
        ),
    )

    index = 0
    while index < len(pool) and (required_concept_refs - covered):
        candidate = pool[index]
        new_coverage = set(candidate.supporting_concept_refs) - covered
        if not new_coverage:
            index += 1
            continue
        selected.append(candidate.object_fqn)
        covered |= new_coverage
        index += 1

    coverage_incomplete = bool(required_concept_refs - covered)

    non_concept_fill_ceiling = min(max_anchors, len(selected) + max(0, max_non_concept_fill))
    already = set(selected)
    for candidate in sorted(non_concept, key=lambda c: (-c.combined_score, c.object_fqn)):
        if len(selected) >= non_concept_fill_ceiling:
            break
        if candidate.object_fqn in already:
            continue
        selected.append(candidate.object_fqn)
        already.add(candidate.object_fqn)

    return [by_fqn[fqn] for fqn in selected], coverage_incomplete


def _preserve_ambiguous_concept_candidates(
    selected: list[_ScoredCandidate], admitted_by_fqn: Mapping[str, _ScoredCandidate],
    concept_supported_admitted: Sequence[_ScoredCandidate], required_concept_refs: frozenset[str],
    family_key_by_fqn_lower: Mapping[str, str], min_ambiguity_confidence: float,
    max_ambiguity_alternatives_per_requirement: int,
) -> tuple[list[_ScoredCandidate], int]:
    """
    Requirement: "preserve multiple plausible anchors when ambiguity is
    real" — generalized to EVERY concept kind (entity, measure, dimension,
    date_role, identifier), not entities alone. For each NORMALIZED
    requirement actually covered by a selected anchor, up to
    `max_ambiguity_alternatives_per_requirement` OTHER admitted candidates
    also supporting that SAME requirement are added too — Requirement 4's
    four justifications:

      1. Materially plausible ambiguity: within ANCHOR_AMBIGUITY_MARGIN of
         the best selected supporter, itself at least `min_ambiguity_
         confidence`, and (when family membership is known for both) NOT
         in the same canonical family/object-type as the best one — two
         members of the same family are a duplicate/narrow-variant
         relationship, not a genuine business ambiguity.
      2/3. Covering another distinct requirement, or independent lexical/
         semantic/example evidence: already guaranteed structurally — such
         a candidate earns its OWN place via coverage (Phase 1) or the
         non-concept fill pass (Phase 2) in _select_anchors, never through
         this function.
      4. "A later graph planner may need them as relationship
         alternatives": IDENTIFIER-kind requirements are structurally JOIN
         KEYS, not single "pick the one right answer" business concepts —
         a real audit found the strict margin rule above correctly
         suppressed a genuinely-needed join table (e.g. a class-signup
         table matched only via a generic "StudentID"-shaped identifier)
         simply because the canonical student table ranked far higher
         overall. For this kind ONLY, retention is instead "top-N by the
         candidate's OWN combined_score, gated by min_ambiguity_confidence,
         no margin requirement" — still bounded by
         `max_ambiguity_alternatives_per_requirement` (never "every table
         containing the same FK column" — that bound is exactly what
         prevents the pre-Step-2B explosion from recurring here).

    Deliberately does NOT truncate to any output bound itself — the caller
    (link_question_to_schema) applies the max_anchor_candidates CEILING
    exactly once, after this step, so it can accurately report whether the
    ceiling was actually reached (`anchor_ceiling_reached`).

    Returns (selected, alternatives_added_count).
    """
    selected_fqns = {c.object_fqn for c in selected}
    by_concept: dict[str, list[_ScoredCandidate]] = defaultdict(list)
    for candidate in concept_supported_admitted:
        for ref in candidate.supporting_concept_refs:
            if ref in required_concept_refs:
                by_concept[ref].append(candidate)

    alternatives_added = 0
    for concept_ref, supporters in by_concept.items():
        selected_supporters = [c for c in supporters if c.object_fqn in selected_fqns]
        if not selected_supporters:
            continue

        if concept_ref.startswith(f"{CONCEPT_KIND_IDENTIFIER}:"):
            candidate_pool = sorted(
                (c for c in supporters if c.object_fqn not in selected_fqns and c.combined_score >= min_ambiguity_confidence),
                key=lambda c: (-c.combined_score, c.object_fqn),
            )
        else:
            plausible = _materially_plausible_competitors(
                supporters, family_key_by_fqn_lower, min_ambiguity_confidence,
                confidence_of=lambda c: c.combined_score, fqn_of=lambda c: c.object_fqn, type_of=lambda c: c.object_type,
            )
            candidate_pool = [c for c in plausible if c.object_fqn not in selected_fqns]

        added_for_this_requirement = 0
        for candidate in candidate_pool:
            if added_for_this_requirement >= max_ambiguity_alternatives_per_requirement:
                break
            selected.append(candidate)
            selected_fqns.add(candidate.object_fqn)
            added_for_this_requirement += 1
            alternatives_added += 1

    return selected, alternatives_added


# ── Ambiguity detection ─────────────────────────────────────────────────


def _detect_ambiguities(
    *, operation: str, entity_requirements: Sequence[MatchedRequirement], measure_requirements: Sequence[MatchedRequirement],
    date_role_requirements: Sequence[MatchedRequirement], selected: Sequence[_ScoredCandidate],
    admitted_by_fqn: Mapping[str, _ScoredCandidate], family_key_by_fqn_lower: Mapping[str, str],
    min_ambiguity_confidence: float,
) -> list[AmbiguityRecord]:
    """
    Phase 6, Step 2A/2B: every ambiguity record below requires MATERIALLY
    PLAUSIBLE competing candidates — restricted to already-ADMITTED
    (relevance-qualified) objects, each individually at least
    `min_ambiguity_confidence`, within ANCHOR_AMBIGUITY_MARGIN of the best,
    and (family info permitting) in DISTINCT canonical families. Since
    matching is now normalized (Step 2B), a requirement whose OTHER
    candidate objects never even cleared admission (the "many objects
    share one common weak token" case this rule exists to suppress)
    produces NO ambiguity record at all.
    """
    records: list[AmbiguityRecord] = []

    for requirement in entity_requirements:
        candidates = [admitted_by_fqn[c.object_fqn] for c in requirement.candidates if c.object_fqn in admitted_by_fqn]
        plausible = _materially_plausible_competitors(
            candidates, family_key_by_fqn_lower, min_ambiguity_confidence,
            confidence_of=lambda c: c.combined_score, fqn_of=lambda c: c.object_fqn, type_of=lambda c: c.object_type,
        )
        if len(plausible) > 1:
            records.append(
                AmbiguityRecord(
                    kind=AMBIGUITY_MULTIPLE_ENTITY_CANDIDATES,
                    description=f"business term maps to {len(plausible)} materially plausible objects for concept {requirement.normalized_label!r}",
                    candidate_refs=tuple(c.object_fqn for c in plausible),
                    confidences=tuple(c.combined_score for c in plausible),
                )
            )

    for requirement in date_role_requirements:
        # A single requirement's candidates already span potentially many
        # (object, column) pairs; date-role AMBIGUITY here is about the
        # requirement's own admitted, confident candidates spanning more
        # than one DISTINCT technical role label (e.g. "started" vs
        # "completed") for objects the question is actually relevant to.
        admitted_candidates = [
            c for c in requirement.candidates
            if c.object_fqn in admitted_by_fqn and c.confidence >= min_ambiguity_confidence
        ]
        distinct_roles = sorted({c.technical_label for c in admitted_candidates})
        if len(distinct_roles) > 1:
            records.append(
                AmbiguityRecord(
                    kind=AMBIGUITY_MULTIPLE_DATE_ROLES,
                    description=f"{len(distinct_roles)} plausible date roles matched for concept {requirement.normalized_label!r}: {distinct_roles!r}",
                    candidate_refs=tuple(f"{requirement.concept_key}:{role}" for role in distinct_roles),
                    confidences=tuple(
                        max((c.confidence for c in admitted_candidates if c.technical_label == role), default=0.0)
                        for role in distinct_roles
                    ),
                )
            )

    aggregation_operations = frozenset({
        OPERATION_SUM, OPERATION_AVERAGE, OPERATION_MINIMUM, OPERATION_MAXIMUM, OPERATION_GROUPED_AGGREGATION,
    })
    admitted_measure_requirements = [
        r for r in measure_requirements
        if any(c.object_fqn in admitted_by_fqn for c in r.candidates) and r.confidence >= min_ambiguity_confidence
    ]
    if operation in aggregation_operations and len(admitted_measure_requirements) > 1:
        confidences = [r.confidence for r in admitted_measure_requirements]
        top = max(confidences)
        close = [r for r, c in zip(admitted_measure_requirements, confidences) if top - c <= ANCHOR_AMBIGUITY_MARGIN]
        if len(close) > 1:
            records.append(
                AmbiguityRecord(
                    kind=AMBIGUITY_UNCLEAR_AGGREGATION_TARGET,
                    description=f"{len(close)} plausible measures for a {operation} question, none clearly dominant",
                    candidate_refs=tuple(r.concept_key for r in close),
                    confidences=tuple(r.confidence for r in close),
                )
            )
        else:
            records.append(
                AmbiguityRecord(
                    kind=AMBIGUITY_MULTIPLE_MEASURES,
                    description=f"{len(admitted_measure_requirements)} distinct measures matched for a {operation} question",
                    candidate_refs=tuple(r.concept_key for r in admitted_measure_requirements),
                    confidences=tuple(confidences),
                )
            )

    ranked = sorted(selected, key=lambda c: (-c.combined_score, c.object_fqn))
    if len(ranked) >= 2:
        top, second = ranked[0], ranked[1]
        overlapping = set(top.supporting_concept_refs) & set(second.supporting_concept_refs)
        top_family = family_key_by_fqn_lower.get(top.object_fqn.lower())
        second_family = family_key_by_fqn_lower.get(second.object_fqn.lower())
        same_family = top_family is not None and second_family is not None and top_family == second_family
        if (
            overlapping and not same_family
            and top.combined_score >= min_ambiguity_confidence
            and (top.combined_score - second.combined_score) <= ANCHOR_AMBIGUITY_MARGIN
        ):
            records.append(
                AmbiguityRecord(
                    kind=AMBIGUITY_CONFLICTING_CANONICAL_CANDIDATES,
                    description=f"{top.object_fqn!r} and {second.object_fqn!r} both plausibly satisfy {sorted(overlapping)!r}",
                    candidate_refs=(top.object_fqn, second.object_fqn),
                    confidences=(top.combined_score, second.combined_score),
                )
            )

    return records


# ── Fingerprint (structured content only — never the raw question) ───────


def _to_plain(value: object) -> object:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError(f"schema linker fingerprint: unsupported value type {type(value).__name__!r}")


def _compute_result_fingerprint(requirements: QuestionRequirements, anchors: Sequence[AnchorCandidate], ambiguities: Sequence[AmbiguityRecord]) -> str:
    payload = {"requirements": requirements, "anchors": list(anchors), "ambiguities": list(ambiguities)}
    canonical = json.dumps(_to_plain(payload), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_requirement_fingerprint(requirements: QuestionRequirements) -> str:
    """
    Phase 6, Step 3F, Part 1 — a fingerprint of ONLY the immutable question-
    requirement SET: (concept_kind, concept_key, strength) for every
    matched requirement, plus `operation`/`wants_comparison`/`wants_trend`/
    `wants_grouping` (the other question-language-derived facts). Every
    field that can legitimately differ by retrieval mode — candidates,
    confidences, evidence, ambiguity flags driven by admitted competitors,
    anchors, ambiguity RECORDS — is deliberately EXCLUDED: this must be
    byte-identical across catalog-only, +lexical, +vector, and +example
    calls for the same (question, catalog), since none of those inputs may
    change what the question requires (Part 1). Never includes the raw
    question text itself (only already-matched concept_key strings, which
    carry normalized business meaning, not the user's literal wording) —
    safe to log or persist on its own.
    """
    payload = {
        "operation": requirements.operation,
        "wants_comparison": requirements.wants_comparison,
        "wants_trend": requirements.wants_trend,
        "wants_grouping": requirements.wants_grouping,
        "requirements": sorted(
            (r.concept_kind, r.concept_key, r.strength)
            for r in (
                requirements.entity_concepts + requirements.measure_concepts + requirements.dimension_concepts
                + requirements.date_role_concepts + requirements.identifier_concepts
            )
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Public entry point ─────────────────────────────────────────────────────


def link_question_to_schema(
    question: str,
    semantic_catalog: SemanticCatalog,
    documents: Sequence,
    *,
    semantic_rankings: Sequence = (),
    lexical_rankings: Sequence = (),
    retrieved_examples: Sequence = (),
    limits: SchemaLinkingLimits = DEFAULT_SCHEMA_LINKING_LIMITS,
) -> SchemaLinkingResult:
    """
    Translate `question` into QuestionRequirements plus a ranked, concept-
    coverage-aware list of AnchorCandidate objects drawn from the COMPLETE
    eligible (Tier 1/2) schema — never a fixed top-K pre-filter. Tier 3/4
    objects are excluded by construction (see _LinkerIndex.eligible_fqns_
    lower, built once and reused for every question against this catalog).

    Pure and local: no database, network, embedding, or LLM/provider call
    of any kind. `question` is used only transiently inside this call —
    never stored, logged, or included in the cache key or in this result's
    own `fingerprint`.

    Phase 6, Step 2B: every matched concept is represented ONCE as a
    MatchedRequirement (QuestionRequirements.entity_concepts/measure_
    concepts/dimension_concepts/date_role_concepts/identifier_concepts),
    keyed only by concept kind + normalized business meaning — NEVER by
    object FQN or column — with every plausible (object, column) candidate
    preserved underneath it. A widely-repeated column (e.g. a "ClassID"-
    shaped foreign key on 50 tables) is therefore ONE requirement with 50
    ranked candidates, not 50 independent requirements each demanding its
    own coverage.

    `semantic_rankings`/`lexical_rankings`: any sequence of objects exposing
    `.object_fqn`/`.score` — e.g. core.sqlservice.vector_index.
    VectorSearchResult / core.sqlservice.lexical_index.LexicalSearchResult
    results the caller already computed. These are EVIDENCE, never an
    admission gate: an object absent from both can still become a strong
    anchor purely through catalog evidence (requirement 2), and an object
    present in both but with zero catalog/canonical/example support can
    still be excluded if nothing outranks the bound.

    `retrieved_examples`: any sequence of objects exposing
    `.proposed_object_fqns` (tuple[str, ...]) / `.similarity` (float) — e.g.
    core.sqlservice.sql_example_retrieval.RetrievedSqlExample results.

    `max_anchor_candidates` (via `limits`) is a CEILING, never a target:
    EVERY eligible object is scored before it is ever applied (see
    _score_all_eligible_objects, which always iterates the complete
    `index.eligible_fqns_lower` set), but only already-ADMITTED (relevance-
    qualified — matched concept, or semantic/lexical/example evidence above
    its own configured minimum; canonical authority alone never admits —
    see _admission_reasons) candidates ever reach anchor selection, and a
    normalized requirement needs only ONE strong candidate to be considered
    covered (Step 2B). A narrow question with few admitted objects returns
    FEWER than `max_anchor_candidates` anchors; a question with no
    relevance-qualified object at all returns status
    STATUS_NO_CONFIDENT_ANCHOR instead of padding the result with
    unrelated, purely-canonical-authority objects.

    Raises:
        ValueError: `question` is not a non-empty string, exceeds
            `limits.max_question_characters`, or `documents` contains
            duplicate object_fqn values (case-insensitively).
    """
    t0 = time.perf_counter()

    if not isinstance(question, str):
        raise ValueError("question must be a string")
    trimmed_question = question.strip()
    if not trimmed_question:
        raise ValueError("question must not be empty")
    if len(trimmed_question) > limits.max_question_characters:
        raise ValueError(
            f"question length {len(trimmed_question)} exceeds the maximum of "
            f"{limits.max_question_characters} characters"
        )

    fqns_lower = [d.object_fqn.lower() for d in documents]
    if len(fqns_lower) != len(set(fqns_lower)):
        raise ValueError("documents contains duplicate object_fqn values (case-insensitive)")

    # Phase 6, Step 3E, Part 1 — index construction/lookup is timed
    # SEPARATELY from this question's own matching/scoring work below, and
    # a failure here returns a typed STATUS_INDEX_BUILD_FAILED result
    # instead of letting an exception propagate mid-way through a
    # partially-built index or silently continuing with a truncated answer.
    t_index_start = time.perf_counter()
    try:
        index = _load_linker_index(semantic_catalog, documents)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any index-construction failure is typed, never a raw crash
        index_load_or_build_ms = (time.perf_counter() - t_index_start) * 1000.0
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        empty_requirements = QuestionRequirements(
            operation=OPERATION_UNKNOWN, entity_concepts=(), measure_concepts=(), dimension_concepts=(),
            date_role_concepts=(), identifier_concepts=(), wants_comparison=False, wants_trend=False,
            wants_grouping=False, unmatched_terms=(), unresolved_filter_values=(), ambiguity_signals=(),
        )
        fingerprint = _compute_result_fingerprint(empty_requirements, (), ())
        return SchemaLinkingResult(
            status=STATUS_INDEX_BUILD_FAILED, requirements=empty_requirements, anchors=(), ambiguities=(),
            warnings=(f"schema index construction failed: {type(exc).__name__}: {exc}",), fingerprint=fingerprint,
            eligible_object_count=0, considered_object_count=0, anchor_total_count=0, elapsed_ms=elapsed_ms,
            index_load_or_build_ms=index_load_or_build_ms, question_linking_ms=0.0,
            requirement_fingerprint=_compute_requirement_fingerprint(empty_requirements),
            relevance_qualified_object_count=0, rejected_for_no_relevance_count=0, anchor_selected_count=0,
            anchors_rejected_by_confidence=0, anchor_ceiling_reached=False,
            no_confident_anchor_reason=None,
            raw_catalog_match_count=0, normalized_requirement_count=0, duplicate_matches_merged_count=0,
            common_token_matches_downweighted_count=0, candidate_alternatives_retained_count=0,
            anchors_selected_by_independent_evidence_count=0, anchors_retained_for_ambiguity_count=0,
        )
    index_load_or_build_ms = (time.perf_counter() - t_index_start) * 1000.0
    t_question_start = time.perf_counter()
    warnings: list[str] = []

    if not index.eligible_fqns_lower:
        question_linking_ms = (time.perf_counter() - t_question_start) * 1000.0
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        empty_requirements = QuestionRequirements(
            operation=OPERATION_UNKNOWN, entity_concepts=(), measure_concepts=(), dimension_concepts=(),
            date_role_concepts=(), identifier_concepts=(), wants_comparison=False, wants_trend=False,
            wants_grouping=False, unmatched_terms=(), unresolved_filter_values=(), ambiguity_signals=(),
        )
        fingerprint = _compute_result_fingerprint(empty_requirements, (), ())
        return SchemaLinkingResult(
            status=STATUS_EMPTY_SCHEMA, requirements=empty_requirements, anchors=(), ambiguities=(),
            warnings=(), fingerprint=fingerprint, eligible_object_count=0, considered_object_count=0,
            anchor_total_count=0, elapsed_ms=elapsed_ms,
            index_load_or_build_ms=index_load_or_build_ms, question_linking_ms=question_linking_ms,
            requirement_fingerprint=_compute_requirement_fingerprint(empty_requirements),
            relevance_qualified_object_count=0, rejected_for_no_relevance_count=0, anchor_selected_count=0,
            anchors_rejected_by_confidence=0, anchor_ceiling_reached=False,
            no_confident_anchor_reason=None,
            raw_catalog_match_count=0, normalized_requirement_count=0, duplicate_matches_merged_count=0,
            common_token_matches_downweighted_count=0, candidate_alternatives_retained_count=0,
            anchors_selected_by_independent_evidence_count=0, anchors_retained_for_ambiguity_count=0,
        )

    raw_question_lower = trimmed_question.lower()
    question_tokens = _meaningful_question_tokens(tokenize(trimmed_question))

    operation = _detect_operation(raw_question_lower)
    wants_comparison = bool(_COMPARISON_PATTERN.search(raw_question_lower))
    wants_trend = bool(_TREND_PATTERN.search(raw_question_lower))
    wants_grouping = bool(_GROUPING_PATTERN.search(raw_question_lower))

    # Moved ahead of concept matching (Step 3C): the entity single-token
    # corroboration pass below needs these rank maps — they depend only on
    # the caller-supplied semantic_rankings/lexical_rankings, never on
    # anything matched below, so computing them first changes no behavior
    # for any other consumer.
    semantic_rank_by_fqn_lower: dict[str, int] = {}
    semantic_score_by_fqn_lower: dict[str, float] = {}
    for i, hit in enumerate(semantic_rankings, start=1):
        key = hit.object_fqn.lower()
        semantic_rank_by_fqn_lower.setdefault(key, i)
        semantic_score_by_fqn_lower.setdefault(key, float(hit.score))

    lexical_rank_by_fqn_lower: dict[str, int] = {}
    lexical_score_by_fqn_lower: dict[str, float] = {}
    for i, hit in enumerate(lexical_rankings, start=1):
        key = hit.object_fqn.lower()
        lexical_rank_by_fqn_lower.setdefault(key, i)
        lexical_score_by_fqn_lower.setdefault(key, float(hit.score))

    entity_requirements, substring_suppressed_count, weak_single_token_demoted_count = _match_entities(
        raw_question_lower, question_tokens, index,
        family_key_by_fqn_lower=index.family_key_by_fqn_lower, min_ambiguity_confidence=limits.min_ambiguity_confidence,
        max_candidates_per_requirement=limits.max_candidates_per_requirement,
    )
    # Phase 6, Step 3F, Part 1 — requirement strength (CORE/CONSTRAINT/
    # SUPPORTING/UNRESOLVED) is now determined SOLELY by normalized question
    # language, operation detection, explicit phrase/business-name matches,
    # and semantic-catalog roles (see _classify_entity_strength/_classify_
    # column_concept_strength) — never by semantic_rank_by_fqn_lower/
    # lexical_rank_by_fqn_lower/retrieved_examples, which are computed
    # above only for anchor RANKING (see _score_all_eligible_objects), never
    # consulted here. Step 3E, Part 2 had introduced one narrow "an
    # UNRESOLVED requirement may resolve to CORE when evidence corroborates
    # exactly one of its materially plausible candidates" exception; Step 3F
    # removes it entirely (verified defect 1/2: on a real production schema,
    # adding vector evidence alone still moved CORE 59 -> 136 purely through
    # this exception, changing the question's own required semantic meaning
    # even though the user's question never changed). UNRESOLVED concepts
    # now remain ambiguity/clarification/candidate-search evidence ONLY —
    # never mandatory plan coverage merely because one schema object ranks
    # well under some retrieval mode. `entity_requirements` below is
    # therefore already the FINAL, evidence-independent requirement set;
    # every mode (catalog-only through full-evidence) computes byte-
    # identical (concept_key, strength) pairs for the same (question,
    # catalog) — see `requirement_fingerprint` and Part 6's invariance tests.

    column_requirements, raw_match_count, raw_candidate_count = _match_column_concepts(
        question_tokens, index,
        family_key_by_fqn_lower=index.family_key_by_fqn_lower, min_ambiguity_confidence=limits.min_ambiguity_confidence,
        max_candidates_per_requirement=limits.max_candidates_per_requirement,
        informativeness_floor=limits.common_token_informativeness_floor,
        operation=operation, wants_grouping=wants_grouping, wants_trend=wants_trend,
    )
    measure_requirements = [r for r in column_requirements if r.concept_kind == CONCEPT_KIND_MEASURE]
    dimension_requirements = [r for r in column_requirements if r.concept_kind == CONCEPT_KIND_DIMENSION]
    date_role_requirements = [r for r in column_requirements if r.concept_kind == CONCEPT_KIND_DATE_ROLE]
    identifier_requirements = [r for r in column_requirements if r.concept_kind == CONCEPT_KIND_IDENTIFIER]

    all_requirements = entity_requirements + measure_requirements + dimension_requirements + date_role_requirements + identifier_requirements
    matched_terms_lower = {t.lower() for r in all_requirements for t in r.matched_terms}
    unmatched_terms = tuple(sorted({t for t in question_tokens if t not in matched_terms_lower}))
    unresolved_filter_values = _extract_unresolved_filter_values(trimmed_question)

    entity_requirements_b, _ = _bound(sorted(entity_requirements, key=lambda r: (-r.confidence, r.concept_key)), limits.max_matched_concepts, label="entity_concepts", warnings=warnings)
    measure_requirements_b, _ = _bound(sorted(measure_requirements, key=lambda r: (-r.confidence, r.concept_key)), limits.max_matched_concepts, label="measure_concepts", warnings=warnings)
    dimension_requirements_b, _ = _bound(sorted(dimension_requirements, key=lambda r: (-r.confidence, r.concept_key)), limits.max_matched_concepts, label="dimension_concepts", warnings=warnings)
    date_role_requirements_b, _ = _bound(sorted(date_role_requirements, key=lambda r: (-r.confidence, r.concept_key)), limits.max_matched_concepts, label="date_role_concepts", warnings=warnings)
    identifier_requirements_b, _ = _bound(sorted(identifier_requirements, key=lambda r: (-r.confidence, r.concept_key)), limits.max_matched_concepts, label="identifier_concepts", warnings=warnings)
    unmatched_terms_b, _ = _bound(unmatched_terms, limits.max_unmatched_terms, label="unmatched_terms", warnings=warnings)

    all_requirements_bounded = (
        entity_requirements_b + measure_requirements_b + dimension_requirements_b
        + date_role_requirements_b + identifier_requirements_b
    )
    # Phase 6, Step 3E, Part 5 — Phase 1's "coverage" anchor selection
    # (_select_anchors) must only chase concepts that actually demand
    # mandatory coverage: CORE (an explicit business ask) and CONSTRAINT
    # (an explicit filter/grouping modifier of one). A raised alias
    # vocabulary (Part 3) or a broader retrieval build routinely produces
    # dozens of SUPPORTING/UNRESOLVED single-candidate entity concepts
    # (incidental borrowed-alias matches on unrelated tables) — treating
    # those as independently "required" forces Phase 1 to select one
    # dedicated anchor PER such concept purely because each has only one
    # possible candidate, silently recreating an anchor-count explosion
    # through this door even though no requirement's STRENGTH actually
    # demands it. SUPPORTING/UNRESOLVED candidates remain fully available
    # for ranking/join-hint purposes and for the bounded non-concept/
    # ambiguity-preservation fill passes — they are simply never mandatory.
    required_concept_refs = frozenset(
        r.concept_key for r in all_requirements_bounded if r.strength in (STRENGTH_CORE, STRENGTH_CONSTRAINT)
    )

    # ── Step 2B observability: raw matches vs. normalized requirements ───
    normalized_requirement_count = len(all_requirements_bounded)
    raw_catalog_match_count = raw_match_count + sum(len(r.candidates) for r in entity_requirements)
    total_unbounded_candidates = raw_candidate_count + sum(r.candidate_total_count for r in entity_requirements)
    duplicate_matches_merged_count = max(0, raw_catalog_match_count - total_unbounded_candidates)
    common_token_matches_downweighted_count = sum(
        1 for r in all_requirements_bounded for c in r.candidates if c.informativeness < 1.0
    )

    # ── Step 3C observability: requirement-strength breakdown ────────────
    core_requirement_count = sum(1 for r in all_requirements_bounded if r.strength == STRENGTH_CORE)
    constraint_requirement_count = sum(1 for r in all_requirements_bounded if r.strength == STRENGTH_CONSTRAINT)
    supporting_requirement_count = sum(1 for r in all_requirements_bounded if r.strength == STRENGTH_SUPPORTING)
    unresolved_requirement_count = sum(1 for r in all_requirements_bounded if r.strength == STRENGTH_UNRESOLVED)

    # Only similarities that clear min_example_similarity ever enter the
    # noisy-OR combination — a low-similarity/irrelevant example can never
    # contribute to example_score (and therefore never to admission or to
    # combined_score) at all (requirement: "irrelevant/low-score example
    # rejected").
    example_confidences_by_fqn_lower: dict[str, list[float]] = defaultdict(list)
    for retrieved in retrieved_examples:
        similarity = _clip01(float(retrieved.similarity))
        if similarity < limits.min_example_similarity:
            continue
        for fqn in retrieved.proposed_object_fqns:
            example_confidences_by_fqn_lower[fqn.lower()].append(similarity)

    scored = _score_all_eligible_objects(
        index, all_requirements_bounded, semantic_rank_by_fqn_lower, semantic_score_by_fqn_lower,
        lexical_rank_by_fqn_lower, lexical_score_by_fqn_lower, example_confidences_by_fqn_lower,
        max_evidence_items_per_anchor=limits.max_evidence_items_per_anchor,
        min_semantic_score=limits.min_semantic_score, max_lexical_admission_rank=limits.max_lexical_admission_rank,
    )
    anchor_total_count = len(scored)

    # ── Stage 1: question-relevance QUALIFICATION (admission) — canonical
    #    authority alone can never admit an object (Step 2A's core rule);
    #    see _admission_reasons for the four real relevance signals. ─────
    admitted = [c for c in scored if c.is_admitted]
    admitted_by_fqn = {c.object_fqn: c for c in admitted}
    relevance_qualified_object_count = len(admitted)
    rejected_for_no_relevance_count = len(scored) - relevance_qualified_object_count

    # Phase 6, Step 6I, Part 2 — a strong, directly-evidenced SUPPORTING
    # candidate may also be treated as anchor-mandatory (see
    # _qualifying_supporting_concept_refs' own docstring for the full,
    # generic eligibility bar). This EXPANDS required_concept_refs in
    # place; nothing about a promoted requirement's own `strength` (still
    # SUPPORTING) or core.sqlservice.join_planner's role mapping changes —
    # only whether _select_anchors/_preserve_ambiguous_concept_candidates
    # treat its concept_key as guaranteed coverage rather than leaving it
    # entirely to the small, generic non-concept fill pass.
    admitted_fqns_lower = frozenset(c.object_fqn.lower() for c in admitted)
    supporting_anchor_promotions = _qualifying_supporting_concept_refs(
        all_requirements_bounded, admitted_fqns_lower, index.eligible_fqns_lower,
    )
    required_concept_refs = required_concept_refs | supporting_anchor_promotions
    supporting_anchor_promotions_count = len(supporting_anchor_promotions)

    # ── Stage 2: canonical/relevance scoring + concept-coverage selection —
    #    operates ONLY over `admitted` from here on, one requirement needs
    #    only ONE strong candidate to be covered (Step 2B). ──────────────
    #
    # Phase 6, Step 6G, Part 3 — per-(concept, object) REQUIREMENT
    # confidence, threaded into _select_anchors so it can rank a candidate
    # by how strongly it satisfies the SPECIFIC concept it is meant to
    # cover, never only by its own overall (corpus-wide, semantic/lexical-
    # rank-blended) combined_score. `dictionary_candidate_outranks_model_
    # only_candidate` and every other existing precedence rule about WHICH
    # candidate a requirement itself prefers is completely unaffected —
    # this only feeds an ALREADY-computed confidence value into a
    # DIFFERENT downstream ranking decision (anchor selection ORDER), never
    # changes it.
    requirement_confidence_by_concept_and_fqn: dict[tuple[str, str], float] = {
        (r.concept_key, c.object_fqn.lower()): c.confidence for r in all_requirements_bounded for c in r.candidates
    }
    selected, coverage_incomplete = _select_anchors(
        admitted, required_concept_refs, limits.max_anchor_candidates, limits.max_non_concept_anchor_fill,
        requirement_confidence_by_concept_and_fqn=requirement_confidence_by_concept_and_fqn,
    )
    if coverage_incomplete:
        warnings.append("not every matched concept is covered by an admitted (relevance-qualified) object")

    concept_supported_admitted = [c for c in admitted if c.supporting_concept_refs]
    pre_ambiguity_fqns = {c.object_fqn for c in selected}
    selected, alternatives_added = _preserve_ambiguous_concept_candidates(
        selected, admitted_by_fqn, concept_supported_admitted, required_concept_refs,
        index.family_key_by_fqn_lower, limits.min_ambiguity_confidence,
        limits.max_ambiguity_alternatives_per_requirement,
    )
    ambiguity_added_fqns = {c.object_fqn for c in selected} - pre_ambiguity_fqns

    selected_sorted = sorted(selected, key=lambda c: (-c.combined_score, c.object_fqn))
    anchor_ceiling_reached = len(selected_sorted) > limits.max_anchor_candidates
    if anchor_ceiling_reached:
        warnings.append(
            f"anchors: {len(selected_sorted) - limits.max_anchor_candidates} item(s) truncated "
            f"by max_items={limits.max_anchor_candidates}"
        )
    anchors_at_ceiling = selected_sorted[:limits.max_anchor_candidates]

    # Final anchor-confidence floor — a conservative safety net (admission
    # already required real evidence, so this rarely fires in practice).
    anchors_bounded = tuple(c for c in anchors_at_ceiling if c.combined_score >= limits.min_anchor_confidence)
    anchors_rejected_by_confidence = len(anchors_at_ceiling) - len(anchors_bounded)

    # Step 3C, requirement 8 — "do not create ambiguity from numerous weak
    # supporting matches": only CORE (and CONSTRAINT, which is still an
    # explicit, deliberate question signal) requirements may ever produce
    # an AMBIGUITY_* record here; a SUPPORTING/UNRESOLVED requirement's own
    # multiple weak candidates are exactly the noise this rule exists to
    # silence.
    _ambiguity_eligible_roles = (STRENGTH_CORE, STRENGTH_CONSTRAINT)
    ambiguity_records = _detect_ambiguities(
        operation=operation,
        entity_requirements=tuple(r for r in entity_requirements_b if r.strength in _ambiguity_eligible_roles),
        measure_requirements=tuple(r for r in measure_requirements_b if r.strength in _ambiguity_eligible_roles),
        date_role_requirements=tuple(r for r in date_role_requirements_b if r.strength in _ambiguity_eligible_roles),
        selected=selected_sorted,
        admitted_by_fqn=admitted_by_fqn, family_key_by_fqn_lower=index.family_key_by_fqn_lower,
        min_ambiguity_confidence=limits.min_ambiguity_confidence,
    )
    ambiguity_records_bounded, _ = _bound(ambiguity_records, limits.max_ambiguity_records, label="ambiguities", warnings=warnings)
    ambiguity_signals = tuple(sorted(f"{r.kind}:{r.description.split(' ')[0]}" for r in ambiguity_records_bounded))

    anchors = tuple(
        AnchorCandidate(
            object_fqn=c.object_fqn, object_type=c.object_type, retrieval_tier=c.retrieval_tier,
            supporting_concepts=c.supporting_concept_refs, supporting_columns=c.supporting_columns,
            semantic_score=c.semantic_score, lexical_score=c.lexical_score, catalog_score=c.catalog_score,
            example_score=c.example_score, canonical_score=c.canonical_score,
            model_semantic_score=c.model_semantic_score, combined_score=c.combined_score,
            evidence=c.evidence, confidence=c.combined_score, admission_reasons=c.admission_reasons,
        )
        for c in anchors_bounded
    )

    anchors_selected_by_independent_evidence_count = sum(
        1 for a in anchors
        if set(a.admission_reasons) & {ADMISSION_REASON_LEXICAL_MATCH, ADMISSION_REASON_SEMANTIC_MATCH, ADMISSION_REASON_EXAMPLE_MATCH}
    )
    final_fqns = {a.object_fqn for a in anchors}
    anchors_retained_for_ambiguity_count = len(ambiguity_added_fqns & final_fqns)

    requirements = QuestionRequirements(
        operation=operation, entity_concepts=entity_requirements_b, measure_concepts=measure_requirements_b,
        dimension_concepts=dimension_requirements_b, date_role_concepts=date_role_requirements_b,
        identifier_concepts=identifier_requirements_b, wants_comparison=wants_comparison, wants_trend=wants_trend,
        wants_grouping=wants_grouping, unmatched_terms=unmatched_terms_b,
        unresolved_filter_values=unresolved_filter_values, ambiguity_signals=ambiguity_signals,
    )

    if relevance_qualified_object_count == 0:
        status = STATUS_NO_CONFIDENT_ANCHOR
        no_confident_anchor_reason = (
            "no eligible object matched a question-relevance signal "
            "(catalog concept, semantic, lexical, or example evidence)"
        )
    elif not anchors_bounded:
        status = STATUS_NO_CONFIDENT_ANCHOR
        no_confident_anchor_reason = (
            f"relevance-qualified objects existed but none met the minimum anchor confidence "
            f"({limits.min_anchor_confidence})"
        )
    else:
        status = STATUS_OK
        no_confident_anchor_reason = None

    warnings_bounded, _ = _bound(warnings, limits.max_warnings, label="warnings", warnings=[])
    fingerprint = _compute_result_fingerprint(requirements, anchors, ambiguity_records_bounded)
    requirement_fingerprint = _compute_requirement_fingerprint(requirements)
    question_linking_ms = (time.perf_counter() - t_question_start) * 1000.0
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    return SchemaLinkingResult(
        status=status, requirements=requirements, anchors=anchors, ambiguities=ambiguity_records_bounded,
        warnings=warnings_bounded, fingerprint=fingerprint, eligible_object_count=len(index.eligible_fqns_lower),
        considered_object_count=len(scored), anchor_total_count=anchor_total_count, elapsed_ms=elapsed_ms,
        index_load_or_build_ms=index_load_or_build_ms, question_linking_ms=question_linking_ms,
        requirement_fingerprint=requirement_fingerprint,
        relevance_qualified_object_count=relevance_qualified_object_count,
        rejected_for_no_relevance_count=rejected_for_no_relevance_count,
        anchor_selected_count=len(anchors), anchors_rejected_by_confidence=anchors_rejected_by_confidence,
        anchor_ceiling_reached=anchor_ceiling_reached, no_confident_anchor_reason=no_confident_anchor_reason,
        raw_catalog_match_count=raw_catalog_match_count, normalized_requirement_count=normalized_requirement_count,
        duplicate_matches_merged_count=duplicate_matches_merged_count,
        common_token_matches_downweighted_count=common_token_matches_downweighted_count,
        candidate_alternatives_retained_count=alternatives_added,
        anchors_selected_by_independent_evidence_count=anchors_selected_by_independent_evidence_count,
        anchors_retained_for_ambiguity_count=anchors_retained_for_ambiguity_count,
        core_requirement_count=core_requirement_count,
        constraint_requirement_count=constraint_requirement_count,
        supporting_requirement_count=supporting_requirement_count,
        unresolved_requirement_count=unresolved_requirement_count,
        substring_suppressed_count=substring_suppressed_count,
        weak_single_token_demoted_count=weak_single_token_demoted_count,
        supporting_anchor_promotions_count=supporting_anchor_promotions_count,
        promoted_supporting_concept_keys=supporting_anchor_promotions,
    )
