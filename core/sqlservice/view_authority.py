"""
Deterministic business-view authority scoring — SQL Architecture
Correction, view-first business-source Step.

Root cause this module addresses (confirmed against a real production
build during this Step's own audit, never against any hardcoded name): a
business-ready VIEW that denormalizes
several base tables can fail every existing admission path (semantic
vector search, lexical search, AND core.sqlservice.schema_linker's own
catalog/dictionary concept matching) simply because it carries no curated
business_name/description and its own column names don't happen to share
a literal word with the question — even though its REAL columns and
confirmed relationships plainly cover the business concepts the question
already matched elsewhere. This module never re-derives retrieval,
ranking, or the linker's own evidence-fusion formula; it adds exactly one
new, independent evidence channel — mirroring the SAME architectural
precedent core.sqlservice.schema_linker's own model-inferred-evidence
channel (Step 4B1) already established: an object with weak-or-no
traditional evidence may still become a candidate through a separate,
independently-scored channel, appended to (never replacing or reordering)
whatever candidates already exist for that requirement.

Pure, in-memory, no I/O of any kind — no database connection, no
embedding/LLM/provider call. Reuses core.sqlservice.canonical_scoring's
already-computed per-object authority score and core.sqlservice.
lexical_index's tokenizer; never re-implements either. No single
customer's specific table/view name, question text, or golden-case answer
appears anywhere in this file (see tests/test_sqlservice_view_authority.py
::test_no_domain_specific_tokens_in_production_module for an enforced
guard, mirroring core.sqlservice.canonical_scoring's own convention).

Integration point: core.sqlservice.schema_plan_options calls
score_business_views()/augment_linking_with_view_authority() once, right
after link_question_to_schema() succeeds and before
plan_schema_join_subgraph() runs — the join planner's own existing
scoring/search is never modified; it simply sees a few more candidates
under whichever MatchedRequirement they matched, and decides, exactly as
it already does for every other candidate, whether including one actually
produces a better plan than the table-only alternatives.
"""
from __future__ import annotations

import dataclasses
import math
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Mapping, Sequence

from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, load_canonical_scores
from core.sqlservice.canonical_scoring import COMPONENT_BUSINESS_DOCUMENTATION as _CANONICAL_BUSINESS_DOC
from core.sqlservice.lexical_index import tokenize
from core.sqlservice.schema_linker import EvidenceItem, RequirementCandidate, SchemaLinkingResult
from core.sqlservice.semantic_catalog import ModelInferredObjectEvidence, SemanticCatalog

__all__ = [
    "STATUS_OK",
    "MATCH_KIND_VIEW_AUTHORITY",
    "COMPONENT_REQUIREMENT_COVERAGE",
    "COMPONENT_CANONICAL_QUALITY",
    "COMPONENT_CONSOLIDATION",
    "COMPONENT_COLUMN_COMPLETENESS",
    "COMPONENT_BUSINESS_DOCUMENTATION_BONUS",
    "COMPONENT_DOMAIN_MATCH",
    "COMPONENT_SOURCE_ROLE_BONUS",
    "COMPONENT_WEIGHTS",
    "SOURCE_ROLE_BONUS_BY_ROLE",
    "infer_candidate_domains",
    "MAX_VIEW_CANDIDATES_PER_REQUIREMENT",
    "MAX_VIEWS_CONSIDERED",
    "CONSOLIDATION_SATURATION",
    "COLUMN_COMPLETENESS_SATURATION",
    "MIN_TOKEN_LENGTH",
    "ViewAuthorityComponents",
    "ViewAuthorityCandidate",
    "ViewAuthorityResult",
    "score_business_views",
    "augment_linking_with_view_authority",
    "clear_view_authority_cache",
    "view_authority_cache_size",
]

STATUS_OK = "OK"

# The ONLY match_kind every candidate this module ever produces carries —
# a caller can always tell "this came from real linker structural/model
# evidence" from "this came from independently-scored view evidence"
# purely from this string, before even looking at confidence.
MATCH_KIND_VIEW_AUTHORITY = "VIEW_AUTHORITY"

COMPONENT_REQUIREMENT_COVERAGE = "REQUIREMENT_COVERAGE"
COMPONENT_CANONICAL_QUALITY = "CANONICAL_QUALITY"
COMPONENT_CONSOLIDATION = "CONSOLIDATION"
COMPONENT_COLUMN_COMPLETENESS = "COLUMN_COMPLETENESS"
COMPONENT_BUSINESS_DOCUMENTATION_BONUS = "BUSINESS_DOCUMENTATION_BONUS"
# Phase 6, Step 6F, Part 5/6 -- two NEW independently-bounded [0, 1]
# components sourced from the current VIEW_ENRICHED cache (via the
# SemanticCatalog's own model_inferred_object_evidence, never a second,
# independently-loaded copy of it). Both are 0.0 whenever no enrichment
# exists for a view at all (`catalog` omitted, or the view was never
# enriched) -- a view's score never depends on these two alone (Part 6:
# "authority is only one bounded component of the final relevance score";
# both are additionally gated behind the SAME MIN_REQUIREMENT_COVERAGE
# real-overlap floor every OTHER component already requires -- see
# score_business_views' own overlap gate).
COMPONENT_DOMAIN_MATCH = "DOMAIN_MATCH"
COMPONENT_SOURCE_ROLE_BONUS = "SOURCE_ROLE_BONUS"

# Independently bounded [0, 1] terms, weights summing to 1.0 — the same
# "no single signal dominates" convention core.sqlservice.canonical_
# scoring.COMPONENT_WEIGHTS and core.sqlservice.schema_linker's own
# evidence-fusion formula already establish. REQUIREMENT_COVERAGE carries
# the largest weight: a view that shows ZERO real overlap with what the
# question actually asked for never becomes a candidate at all (see
# score_business_views' own "overlap" gate) — this weight only shapes
# ranking AMONG views that already cleared that gate. CANONICAL_QUALITY
# reuses core.sqlservice.canonical_scoring's already-vetted, already-
# tier/PK/FK/connectivity-aware authority score rather than re-deriving
# any of those signals a second time. CONSOLIDATION is the one genuinely
# NEW signal this module contributes: canonical scoring has no notion of
# "this object already denormalizes what would otherwise be several
# separate joins" (requirement: "whether one view replaces a larger
# multi-table join").
COMPONENT_WEIGHTS: Mapping[str, float] = {
    COMPONENT_REQUIREMENT_COVERAGE: 0.30,
    COMPONENT_CANONICAL_QUALITY: 0.15,
    COMPONENT_CONSOLIDATION: 0.15,
    COMPONENT_COLUMN_COMPLETENESS: 0.10,
    COMPONENT_BUSINESS_DOCUMENTATION_BONUS: 0.10,
    COMPONENT_DOMAIN_MATCH: 0.10,
    COMPONENT_SOURCE_ROLE_BONUS: 0.10,
}
assert abs(sum(COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9

# Generic, contract-level (never customer-specific) source_role -> bonus
# mapping. AUTHORITATIVE_BUSINESS_VIEW/CURRENT_STATE_VIEW are treated as
# equally strong "this IS the current, intended business source" signals;
# HISTORICAL_VIEW earns a reduced bonus (Part 6: "current versus historical
# classification" -- a historical view is real evidence, just weaker for a
# CURRENT-state question); SUMMARY_AGGREGATE_VIEW/SUPPORTING_VIEW earn a
# smaller, still-positive bonus (a real, documented business role, just
# not the authoritative source itself). These five values are the
# VIEW_ENRICHED contract's OWN fixed enum (core.sqlservice.
# semantic_enrichment_service._VALID_SOURCE_ROLES) -- not a per-schema or
# per-customer list.
SOURCE_ROLE_BONUS_BY_ROLE: Mapping[str, float] = {
    "AUTHORITATIVE_BUSINESS_VIEW": 1.0,
    "CURRENT_STATE_VIEW": 1.0,
    "SUMMARY_AGGREGATE_VIEW": 0.5,
    "SUPPORTING_VIEW": 0.5,
    "HISTORICAL_VIEW": 0.3,
}

# Defensive, configuration-level bounds — never tuned against any specific
# schema/question. MAX_VIEWS_CONSIDERED bounds the (cheap, but still
# worth capping) scan itself; MAX_VIEW_CANDIDATES_PER_REQUIREMENT bounds
# how many new candidates a single requirement can gain from this module,
# mirroring core.sqlservice.schema_linker.SchemaLinkingLimits' own
# per-requirement candidate bound.
MAX_VIEWS_CONSIDERED = 500
MAX_VIEW_CANDIDATES_PER_REQUIREMENT = 5
# A view whose weighted requirement_coverage falls below this floor is
# dropped even if it shares a raw token — "real overlap" alone is not
# enough once that overlap is weighted by how common the token is across
# the whole view pool (see _requirement_coverage). Small and generic,
# never tuned against any specific schema/question — it only rules out
# coverage so weak it is indistinguishable from a coincidental shared
# common word.
MIN_REQUIREMENT_COVERAGE = 0.15
# "Replacing a join across this many other confirmed objects" already
# earns full consolidation credit — log-saturating, exactly like every
# other count-based term in core.sqlservice.canonical_scoring, so a view
# with 50 confirmed edges never scores proportionally higher than one
# with 6.
CONSOLIDATION_SATURATION = 4
COLUMN_COMPLETENESS_SATURATION = 15
# Generic English short-token filter — the same >= 3 alphabetic-character
# floor core.sqlservice.schema_linker._meaningful_question_tokens already
# applies, reimplemented locally so this module stays fully independent
# (never imports a schema_linker private helper).
MIN_TOKEN_LENGTH = 3


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _saturating(count: int, saturation: int) -> float:
    if count <= 0 or saturation <= 0:
        return 0.0
    return _clip01(math.log1p(count) / math.log1p(saturation))


def _meaningful_tokens(text: str) -> frozenset[str]:
    return frozenset(
        t for t in tokenize(str(text)) if len(t) >= MIN_TOKEN_LENGTH and any(c.isalpha() for c in t)
    )


def _view_tokens(doc, semantic_evidence: ModelInferredObjectEvidence | None = None) -> frozenset[str]:
    """Every real, generic column-name/synonym token this view's OWN
    metadata already carries — never a guess, never anything derived from
    document_text/raw table rows. `semantic_evidence` (Phase 6, Step 6F,
    Part 5/6, optional) ALSO folds in the current VIEW_ENRICHED cache's own
    business_domain/entity_concepts/aliases/status_concept_labels/
    supported_operations candidates -- the SAME mechanism that already lets
    a question overlapping a raw column name earn coverage now ALSO
    recognizes a question overlapping the view's OWN documented business
    vocabulary (e.g. "enrollment status" from a status concept label, even
    when no column is literally named that)."""
    tokens: set[str] = set()
    for name in (doc.metadata.get("column_names") or ()):
        tokens |= _meaningful_tokens(str(name))
    for synonym in (doc.metadata.get("synonyms") or ()):
        tokens |= _meaningful_tokens(str(synonym))
    if semantic_evidence is not None:
        for domain in semantic_evidence.business_domain_candidates:
            tokens |= _meaningful_tokens(domain)
        for concept in semantic_evidence.entity_concept_candidates:
            tokens |= _meaningful_tokens(concept)
        for alias in semantic_evidence.alias_candidates:
            tokens |= _meaningful_tokens(alias)
        for label in semantic_evidence.status_concept_label_candidates:
            tokens |= _meaningful_tokens(label)
        for operation in semantic_evidence.supported_operation_candidates:
            tokens |= _meaningful_tokens(operation)
    return frozenset(tokens)


def infer_candidate_domains(
    all_requirements: Sequence, discovered_domains: frozenset[str],
) -> frozenset[str]:
    """
    Phase 6, Step 6F, Part 5: deterministic, in-memory, NO provider call --
    a business_domain from the catalog's OWN discovered vocabulary
    (`discovered_domains`, never a fixed/hardcoded list) is a CANDIDATE
    question domain exactly when its own tokens overlap with any
    requirement's own normalized_label/matched_terms. Ambiguous by
    construction: returns EVERY domain clearing the bar, never forces a
    single choice ("treat ambiguous domain detection as multiple
    candidates, not a forced decision") -- an empty result means "no
    inferable domain", which callers must treat as "boost nothing", never
    as a reason to filter.
    """
    if not discovered_domains:
        return frozenset()
    requirement_tokens: set[str] = set()
    for requirement in all_requirements:
        requirement_tokens |= _requirement_tokens(requirement)
    if not requirement_tokens:
        return frozenset()
    return frozenset(
        domain for domain in discovered_domains
        if _meaningful_tokens(domain) & requirement_tokens
    )


def _requirement_tokens(requirement) -> frozenset[str]:
    tokens: set[str] = set(_meaningful_tokens(requirement.normalized_label))
    for term in requirement.matched_terms:
        tokens |= _meaningful_tokens(term)
    return frozenset(tokens)


def _token_informativeness(token: str, document_frequency: Mapping[str, int], total_views: int) -> float:
    """Bounded [0, 1] IDF-like weight — 1.0 for a token that names only
    one (or very few) views, decreasing toward 0 as more DISTINCT views
    happen to share it — the SAME generic "structural document frequency,
    never a hardcoded token list" principle core.sqlservice.schema_linker
    already applies to its own column-token informativeness (that module
    is never imported here; this is an independent computation over this
    module's own view pool, following the same convention)."""
    if total_views <= 0:
        return 1.0
    frequency = document_frequency.get(token, 0)
    if frequency <= 0:
        return 1.0
    max_idf = math.log1p(total_views / 2.0) or 1.0
    return _clip01(math.log1p(total_views / frequency) / max_idf)


def _requirement_coverage(
    requirement_tokens: frozenset[str], overlap: frozenset[str],
    document_frequency: Mapping[str, int], total_views: int,
) -> float:
    """The PRODUCT of a plain coverage ratio and the average informativeness
    of the tokens that actually matched — deliberately NOT a
    weighted-overlap / weighted-total RATIO (that self-cancels for the
    single-token requirements a real schema produces constantly: with
    only one requirement token, weighted_overlap and weighted_total are
    the SAME value, so the ratio is always 1.0 regardless of how common
    that one token is — the exact false-positive this function exists to
    prevent). A single-word concept label (e.g. a normalized_label of
    just "survey" or "answer") must NOT earn full coverage credit from
    any view sharing that one common word: `coverage_ratio` still rewards
    matching a LARGER share of a multi-token requirement, while
    `avg_informativeness` independently discounts the credit for HOW
    common the matched token(s) are across the whole view pool — a match
    on a genuinely distinguishing token (e.g. one naming only a handful
    of views) keeps close to full credit; a match on a token dozens of
    views share is discounted toward zero."""
    if not requirement_tokens or not overlap:
        return 0.0
    coverage_ratio = len(overlap) / len(requirement_tokens)
    avg_informativeness = sum(
        _token_informativeness(t, document_frequency, total_views) for t in overlap
    ) / len(overlap)
    return _clip01(coverage_ratio * avg_informativeness)


def _consolidation_count(doc) -> int:
    """Distinct OTHER objects this view has a confirmed relationship to
    (either direction) — a structural proxy for "how many objects this
    view already joins together for the caller", computed only from real
    confirmed_relationship_edges metadata (core.sqlservice.
    retrieval_documents' own provenance-hardened field), never fabricated
    and never a guess from the view's name."""
    related: set[str] = set()
    for entry in (doc.metadata.get("confirmed_relationship_edges") or ()):
        if not isinstance(entry, Mapping):
            continue
        other = str(entry.get("other") or "")
        object_fqn, _, _column = other.rpartition(".")
        if object_fqn:
            related.add(object_fqn.lower())
    return len(related)


# ── Public data model (frozen, deeply immutable) ──────────────────────────


@dataclass(frozen=True)
class ViewAuthorityComponents:
    """Every component independently bounded [0, 1] — see COMPONENT_WEIGHTS'
    own docstring for what each measures and why it is weighted as it is."""

    requirement_coverage: float
    canonical_quality: float
    consolidation: float
    column_completeness: float
    business_documentation_bonus: float
    domain_match: float = 0.0
    source_role_bonus: float = 0.0


@dataclass(frozen=True)
class ViewAuthorityCandidate:
    """One VIEW proposed as an additional candidate for one MatchedRequirement
    (`concept_key`, e.g. "ENTITY:student") the linker already matched.
    `matched_tokens` are the real, shared column-name/synonym tokens this
    candidate's `requirement_coverage` component was computed from —
    exactly the evidence a caller/report can show for "why was this view
    proposed". `score` is the weighted combination of `components`,
    already bounded [0, 1]."""

    object_fqn: str
    concept_key: str
    matched_tokens: tuple[str, ...]
    score: float
    components: ViewAuthorityComponents
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ViewAuthorityResult:
    """Result of one score_business_views() call. `candidates` is already
    bounded (MAX_VIEW_CANDIDATES_PER_REQUIREMENT per requirement) and
    deterministically ordered (descending score, ascending object_fqn
    tie-break) — never re-sorted by a caller. `eligible_view_count` is the
    number of VIEW-type objects in the supplied (already Tier 1/2-filtered)
    document set BEFORE any MAX_VIEWS_CONSIDERED truncation;
    `considered_view_count` is the number actually scanned."""

    status: str
    candidates: tuple[ViewAuthorityCandidate, ...]
    considered_view_count: int
    eligible_view_count: int
    build_id: int | None
    schema_fingerprint: str | None
    warnings: tuple[str, ...]


# ── Bounded, build/fingerprint-keyed view-token-index cache ───────────────


@dataclass(frozen=True)
class _ViewIndexEntry:
    object_fqn: str
    fqn_lower: str
    tokens: frozenset[str]
    column_count: int
    consolidation_count: int
    business_domain: str = ""
    has_business_documentation: bool = False
    source_role_bonus: float = 0.0


@dataclass(frozen=True)
class _ViewIndex:
    """Everything score_business_views needs about the current view pool,
    computed once and cached. `token_document_frequency` — how many
    DISTINCT views carry a given token — is what makes requirement_
    coverage a WEIGHTED (informativeness-adjusted) match rather than a
    naive token-count ratio (see _requirement_coverage's own docstring for
    the exact false-positive this fixes: a single-word concept label like
    "survey" or "answer" is common across dozens of unrelated views in a
    real schema, and without this weighting ANY of them would earn full
    coverage credit purely from that one coincidental word).

    `discovered_domains` (Phase 6, Step 6F, Part 5) is the SET of distinct
    business_domain values actually proposed anywhere in the current
    catalog's model-inferred evidence -- the catalog's OWN vocabulary,
    never a fixed/hardcoded list, and empty whenever no `catalog` was
    supplied to score_business_views() at all."""

    entries: tuple[_ViewIndexEntry, ...]
    token_document_frequency: Mapping[str, int]
    discovered_domains: frozenset[str] = frozenset()


_MAX_CACHE_ENTRIES = 8
_view_index_cache: "OrderedDict[tuple, _ViewIndex]" = OrderedDict()
_cache_lock = RLock()


def _source_role_bonus(object_role_candidates: Sequence[tuple[str, float]]) -> float:
    """Best (role, confidence) pair's bonus, confidence-weighted -- 0.0
    when no role was ever proposed for this view at all (no enrichment, or
    an enrichment whose confidence rounds to 0)."""
    best = 0.0
    for role, confidence in object_role_candidates:
        bonus = SOURCE_ROLE_BONUS_BY_ROLE.get(role, 0.0) * _clip01(confidence)
        best = max(best, bonus)
    return _clip01(best)


def _load_view_token_index(
    views: Sequence, build_id: int | None, schema_fingerprint: str | None,
    catalog: SemanticCatalog | None = None,
) -> _ViewIndex:
    # Part 5: "cached per semantic-catalog identity" -- catalog.fingerprint
    # already changes whenever the current VIEW_ENRICHED cache changes
    # (Part 4's own fingerprint requirement), so including it here means a
    # NEW enrichment run invalidates this module's cache too, without this
    # module needing to re-derive that identity itself. getattr (never a
    # direct attribute access) because some callers pass a minimal/stub
    # catalog-like object that carries no `.fingerprint` at all -- this
    # module must degrade gracefully (an empty cache-key component, and no
    # model-inferred evidence to read), never raise, in that case.
    catalog_fingerprint = getattr(catalog, "fingerprint", "") if catalog is not None else ""
    key = (build_id or 0, schema_fingerprint or "", catalog_fingerprint)
    with _cache_lock:
        cached = _view_index_cache.get(key)
        if cached is not None:
            _view_index_cache.move_to_end(key)
            return cached

    evidence_by_fqn_lower: dict[str, ModelInferredObjectEvidence] = {}
    if catalog is not None:
        for evidence in getattr(catalog, "model_inferred_object_evidence", ()) or ():
            evidence_by_fqn_lower[evidence.object_fqn.lower()] = evidence

    entries = []
    discovered_domains: set[str] = set()
    for d in sorted(views, key=lambda d: d.object_fqn.lower()):
        semantic_evidence = evidence_by_fqn_lower.get(d.object_fqn.lower())
        business_domain = ""
        has_business_documentation = False
        source_role_bonus = 0.0
        if semantic_evidence is not None:
            if semantic_evidence.business_domain_candidates:
                business_domain = semantic_evidence.business_domain_candidates[0]
                discovered_domains.add(business_domain)
            has_business_documentation = bool(
                semantic_evidence.business_name_candidates or semantic_evidence.description_candidates
            )
            source_role_bonus = _source_role_bonus(semantic_evidence.object_role_candidates)
        entries.append(_ViewIndexEntry(
            object_fqn=d.object_fqn, fqn_lower=d.object_fqn.lower(),
            tokens=_view_tokens(d, semantic_evidence),
            column_count=len(d.metadata.get("column_names") or ()), consolidation_count=_consolidation_count(d),
            business_domain=business_domain, has_business_documentation=has_business_documentation,
            source_role_bonus=source_role_bonus,
        ))
    entries = tuple(entries)

    document_frequency: dict[str, int] = {}
    for entry in entries:
        for token in entry.tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1
    index = _ViewIndex(
        entries=entries, token_document_frequency=document_frequency,
        discovered_domains=frozenset(discovered_domains),
    )
    with _cache_lock:
        _view_index_cache[key] = index
        _view_index_cache.move_to_end(key)
        while len(_view_index_cache) > _MAX_CACHE_ENTRIES:
            _view_index_cache.popitem(last=False)
    return index


def clear_view_authority_cache() -> None:
    with _cache_lock:
        _view_index_cache.clear()


def view_authority_cache_size() -> int:
    with _cache_lock:
        return len(_view_index_cache)


# ── Public entry points ────────────────────────────────────────────────────


def score_business_views(
    linking: SchemaLinkingResult, documents: Sequence, *,
    build_id: int | None = None, schema_fingerprint: str | None = None,
    canonical_identity: CanonicalScoringIdentity | None = None,
    catalog: SemanticCatalog | None = None,
    max_views_considered: int = MAX_VIEWS_CONSIDERED,
    max_candidates_per_requirement: int = MAX_VIEW_CANDIDATES_PER_REQUIREMENT,
) -> ViewAuthorityResult:
    """
    Score every VIEW-type object in `documents` (already Tier 1/2-filtered
    by the caller — this module never re-applies tier policy, it trusts
    the same eligible set every other stage already trusts) as a candidate
    for whichever of `linking.requirements`' own MatchedRequirement entries
    it shows real token overlap with.

    A view earns candidacy for a SPECIFIC requirement only when it shares
    at least one real column-name/synonym/business-vocabulary token with
    that requirement's own normalized_label/matched_terms (requirement:
    "a view must earn priority through question relevance and requirement
    coverage" — never a blanket "every view is a candidate for
    everything"). A view already present among that requirement's own
    candidates is skipped for it (never a duplicate, never an override of
    existing structural/model evidence).

    `catalog` (Phase 6, Step 6F, Part 5/6, optional) supplies the current
    VIEW_ENRICHED cache's model-inferred evidence: business_domain/entity_
    concepts/aliases/status_concept_labels/supported_operations widen the
    token pool a view can earn coverage through (Part 5's own "boost
    objects with matching domains, entities, statuses, aliases, and
    supported operations"), and source_role/business-name-or-description
    presence feed two NEW bounded components (COMPONENT_DOMAIN_MATCH/
    COMPONENT_SOURCE_ROLE_BONUS). Omitting `catalog` reproduces this
    function's exact pre-Part-5/6 behavior (both new components are
    always 0.0). A view NEVER enters the candidate pool through these two
    components alone -- the SAME MIN_REQUIREMENT_COVERAGE real-overlap
    gate below still applies first, regardless of `catalog`.

    Archive/backup/old/system/test/deprecated objects are never reached
    here at all: they are excluded by core.sqlservice.retrieval_documents.
    classify_retrieval_tier's existing, generic tier policy, applied
    BEFORE `documents` ever reaches this module (unchanged, never
    weakened or re-implemented here).
    """
    warnings: list[str] = []
    views = [d for d in documents if d.object_type == "VIEW"]
    eligible_view_count = len(views)
    if len(views) > max_views_considered:
        views = sorted(views, key=lambda d: d.object_fqn.lower())[:max_views_considered]
        warnings.append(
            f"views: {eligible_view_count - len(views)} item(s) truncated by "
            f"max_views_considered={max_views_considered}"
        )

    # When no catalog is supplied, COMPONENT_DOMAIN_MATCH/COMPONENT_SOURCE_
    # ROLE_BONUS are 0.0 for EVERY view uniformly (there is no enrichment
    # evidence to compute them from at all) -- rather than silently
    # wasting 20% of the achievable score, redistribute their weight back
    # onto the five original components, proportionally, so a caller that
    # never passes `catalog` gets byte-identical scores to this function's
    # pre-Part-5/6 behavior. A caller that DOES supply `catalog` uses the
    # full 7-component weighting even for one specific view that happens
    # to lack its own enrichment row -- that 0.0 is real, discriminating
    # signal in that case ("no documented business role for THIS view"),
    # not a missing-prerequisite artifact.
    if catalog is not None:
        active_weights = COMPONENT_WEIGHTS
    else:
        _reduced = {
            k: v for k, v in COMPONENT_WEIGHTS.items()
            if k not in (COMPONENT_DOMAIN_MATCH, COMPONENT_SOURCE_ROLE_BONUS)
        }
        _reduced_total = sum(_reduced.values())
        active_weights = {k: v / _reduced_total for k, v in _reduced.items()}

    identity = canonical_identity or CanonicalScoringIdentity(
        build_id=build_id or 0, embedding_provider="view_authority", embedding_model="none", embedding_dimensions=0,
    )
    canonical = load_canonical_scores(documents, identity)
    view_index = _load_view_token_index(views, build_id, schema_fingerprint, catalog)
    total_views = len(view_index.entries)

    all_requirements = (
        linking.requirements.entity_concepts + linking.requirements.measure_concepts
        + linking.requirements.dimension_concepts + linking.requirements.date_role_concepts
        + linking.requirements.identifier_concepts
    )
    # Part 5: multiple candidate domains, never a forced single choice --
    # an empty set (no catalog, or no overlap at all) means "boost
    # nothing", never "filter to one domain".
    candidate_domains = infer_candidate_domains(all_requirements, view_index.discovered_domains)

    candidates: list[ViewAuthorityCandidate] = []
    for requirement in all_requirements:
        requirement_tokens = _requirement_tokens(requirement)
        if not requirement_tokens:
            continue
        already_covered = {c.object_fqn.lower() for c in requirement.candidates}
        for_this_requirement: list[ViewAuthorityCandidate] = []
        for entry in view_index.entries:
            if entry.fqn_lower in already_covered:
                continue
            overlap = requirement_tokens & entry.tokens
            if not overlap:
                continue
            coverage = _requirement_coverage(
                requirement_tokens, overlap, view_index.token_document_frequency, total_views,
            )
            if coverage < MIN_REQUIREMENT_COVERAGE:
                continue
            canonical_score = canonical.scores_by_fqn_lower.get(entry.fqn_lower)
            canonical_quality = canonical_score.total_score if canonical_score is not None else 0.0
            business_doc_bonus = (
                canonical_score.components.get(_CANONICAL_BUSINESS_DOC, 0.0)
                if canonical_score is not None else 0.0
            )
            # A view's OWN VIEW_ENRICHED business_name/description (more
            # directly "documented business purpose" than the older,
            # dictionary-only canonical-scoring signal) also satisfies this
            # component -- whichever signal is stronger wins; neither ever
            # replaces the other.
            if entry.has_business_documentation:
                business_doc_bonus = max(business_doc_bonus, 1.0)
            domain_match = 1.0 if (entry.business_domain and entry.business_domain in candidate_domains) else 0.0
            components = ViewAuthorityComponents(
                requirement_coverage=coverage,
                canonical_quality=_clip01(canonical_quality),
                consolidation=_saturating(entry.consolidation_count, CONSOLIDATION_SATURATION),
                column_completeness=_saturating(entry.column_count, COLUMN_COMPLETENESS_SATURATION),
                business_documentation_bonus=_clip01(business_doc_bonus),
                domain_match=domain_match,
                source_role_bonus=entry.source_role_bonus,
            )
            score = (
                active_weights[COMPONENT_REQUIREMENT_COVERAGE] * components.requirement_coverage
                + active_weights[COMPONENT_CANONICAL_QUALITY] * components.canonical_quality
                + active_weights[COMPONENT_CONSOLIDATION] * components.consolidation
                + active_weights[COMPONENT_COLUMN_COMPLETENESS] * components.column_completeness
                + active_weights[COMPONENT_BUSINESS_DOCUMENTATION_BONUS] * components.business_documentation_bonus
                + active_weights.get(COMPONENT_DOMAIN_MATCH, 0.0) * components.domain_match
                + active_weights.get(COMPONENT_SOURCE_ROLE_BONUS, 0.0) * components.source_role_bonus
            )
            for_this_requirement.append(ViewAuthorityCandidate(
                object_fqn=entry.object_fqn, concept_key=requirement.concept_key,
                matched_tokens=tuple(sorted(overlap)), score=_clip01(score), components=components,
                evidence=(
                    f"matched_tokens={sorted(overlap)}",
                    f"consolidates {entry.consolidation_count} related object(s)",
                    f"{entry.column_count} column(s)",
                ),
            ))
        for_this_requirement.sort(key=lambda c: (-c.score, c.object_fqn.lower()))
        candidates.extend(for_this_requirement[:max_candidates_per_requirement])

    return ViewAuthorityResult(
        status=STATUS_OK, candidates=tuple(candidates), considered_view_count=len(views),
        eligible_view_count=eligible_view_count, build_id=build_id, schema_fingerprint=schema_fingerprint,
        warnings=tuple(warnings),
    )


def augment_linking_with_view_authority(
    linking: SchemaLinkingResult, view_result: ViewAuthorityResult,
) -> SchemaLinkingResult:
    """
    Append `view_result.candidates` to the matching MatchedRequirement's own
    `candidates` tuple — never removes, reorders, or overrides an existing
    candidate; never changes `requirements.confidence`/`ambiguous`/
    `strength` (those remain whatever core.sqlservice.schema_linker itself
    already computed — this module never re-derives them, exactly like
    core.sqlservice.schema_linker's own model-inferred-evidence channel
    "adding model-inferred candidates to an existing requirement never
    changes it"). Only `candidates`/`candidate_total_count` change on an
    affected requirement; every other field of `linking` (anchors,
    ambiguities, warnings, every observability count) is untouched.

    Returns `linking` UNCHANGED (same object) when there is nothing to add
    — a caller can always compare `is` to check whether augmentation did
    anything.
    """
    if not view_result.candidates:
        return linking

    by_concept_key: dict[str, list[ViewAuthorityCandidate]] = {}
    for candidate in view_result.candidates:
        by_concept_key.setdefault(candidate.concept_key, []).append(candidate)

    def _augment(requirements: tuple) -> tuple:
        updated = []
        changed = False
        for requirement in requirements:
            extra = by_concept_key.get(requirement.concept_key)
            if not extra:
                updated.append(requirement)
                continue
            changed = True
            new_candidates = tuple(
                RequirementCandidate(
                    object_fqn=c.object_fqn, column_name=None, technical_label=requirement.normalized_label,
                    match_kind=MATCH_KIND_VIEW_AUTHORITY, confidence=c.score, raw_confidence=c.score,
                    informativeness=1.0,
                    evidence=(EvidenceItem(kind=MATCH_KIND_VIEW_AUTHORITY, detail=", ".join(c.evidence)),),
                )
                for c in extra
            )
            updated.append(dataclasses.replace(
                requirement,
                candidates=requirement.candidates + new_candidates,
                candidate_total_count=requirement.candidate_total_count + len(new_candidates),
            ))
        return tuple(updated), changed

    entity_concepts, c1 = _augment(linking.requirements.entity_concepts)
    measure_concepts, c2 = _augment(linking.requirements.measure_concepts)
    dimension_concepts, c3 = _augment(linking.requirements.dimension_concepts)
    date_role_concepts, c4 = _augment(linking.requirements.date_role_concepts)
    identifier_concepts, c5 = _augment(linking.requirements.identifier_concepts)

    if not (c1 or c2 or c3 or c4 or c5):
        return linking

    new_requirements = dataclasses.replace(
        linking.requirements,
        entity_concepts=entity_concepts, measure_concepts=measure_concepts,
        dimension_concepts=dimension_concepts, date_role_concepts=date_role_concepts,
        identifier_concepts=identifier_concepts,
    )
    return dataclasses.replace(linking, requirements=new_requirements)
