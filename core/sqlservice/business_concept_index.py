"""
Automatic business-concept retrieval — Phase 7, Step 3B, Part 2.

Problem this closes: a business term (e.g. "program") can fail to retrieve
the technically-named schema object it actually refers to (e.g. a table
whose own real name/columns share no vocabulary with that term at all) even
though ToolSmithAI already holds TRUSTED, previously AI-adjudicated evidence
of that exact relationship — it just lives in a
subsystem core.sqlservice's hybrid (vector + lexical) retrieval never reads:
data.entity_service/data.domain_service/data.semantic_contract_service
(entity_assignments, domain_assignments, semantic_entity_contracts) plus
core.sqlservice's own OBJECT_CORE/VIEW_ENRICHED enrichment stores.

This module builds a per-source, per-build, immutable, source-scoped lexical
index over exactly that trusted evidence and searches it with the SAME
deterministic BM25F machinery core.sqlservice.lexical_index already uses for
schema retrieval (never new ranking math, never a new vector database, never
a provider/embedding call). core.sqlservice.schema_context_retrieval folds
the result in as one more reciprocal-rank-fusion term alongside the existing
semantic/lexical ranks — an ADDITIONAL relevance lane, never an override: an
object with no matched business-concept evidence simply gets no rank here at
all (exactly how a lexical miss already works), so this can only ever help a
question surface an object it has real, trusted, generic evidence for.

Trust policy (every input read-only, from already-persisted, already
AI-adjudicated/rule-classified metadata — nothing is generated here):
  - OBJECT_CORE (core.sqlservice.semantic_enrichment_store): current (hash-
    matched), ACTIVE rows only — aliases, business_name, entity_concepts.
  - VIEW_ENRICHED (core.sqlservice.semantic_enrichment_store): current,
    ACTIVE rows only, Tier 1/2 views only — aliases, business_domain,
    status_concepts.
  - entity_assignments / domain_assignments (data.entity_service /
    data.domain_service): rows with a real (non-"Unknown", non-blank) label
    and assignment_source != "preliminary_connect_time" (the same
    unvalidated-provenance exclusion core.semantic.ai_interpreter.
    build_grounding_vocabulary already applies via exclude_preliminary).
  - semantic_entity_contracts (data.semantic_contract_service): only the
    latest contract_version per entity_name, and only when
    resolution_status == "RESOLVED" — a PARTIAL or NO_CANDIDATE contract is
    not yet trusted evidence of a real object mapping. A contract attribute
    naming a column that no longer exists on the real object (per this
    build's own retrieval documents) is dropped rather than trusted blindly.
  - Every source above is additionally intersected with this build's own
    Tier 1/2, currently-indexed object set — a Tier 3/4/archive/backup/
    system object, or an object no longer in the schema, can never enter
    this index regardless of what any of these tables claim about it.

No database-specific, golden-question, or any other hardcoded object/business
term appears anywhere in this file — every mapping is read generically from
whatever the four trust sources above actually contain for the given
source_id.

Cache identity ties a build's fingerprint to a cheap, indexed enrichment
"state" signature (row count + latest updated_at across all five source
tables) — mirrors core.sqlservice.schema_plan_options._view_enrichment_
cache_state exactly. Same build + unchanged enrichment state (an ordinary
reconnect to the identical schema) is a cache hit; a NEW enrichment/
adjudication run, or a real schema change (new build_id), invalidates it
automatically. Nothing here ever persists the question text or a query
vector — the index itself never sees a question until search time, and the
cache stores only the built BusinessConceptIndex object, in-process.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Mapping, Sequence

from data.db import get_connection
from data.domain_service import list_domain_assignments
from data.entity_service import list_entity_assignments
from data.semantic_contract_service import get_latest_entity_contract

from core.sqlservice.lexical_index import (
    LexicalIndex,
    LexicalIndexIdentity,
    build_lexical_index,
    search_lexical_index,
    tokenize,
)
from core.sqlservice.semantic_enrichment_phases import PHASE_OBJECT_CORE
from core.sqlservice.semantic_enrichment_providers import OpenAISemanticEnrichmentProvider
from core.sqlservice.semantic_enrichment_service import (
    SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
    VIEW_ENRICHMENT_CONTRACT_VERSION,
    build_object_semantic_inputs,
    compute_object_core_cache_key,
    compute_view_cache_key,
)
from core.sqlservice.semantic_enrichment_store import (
    STATUS_ACTIVE,
    load_current_enrichments_for_source,
    load_current_view_enrichments_for_source,
)
from core.sqlservice.view_semantic_enrichment_preparation import build_eligible_view_inputs

__all__ = [
    "MatchedConceptEvidence",
    "BusinessConceptIndexIdentity",
    "BusinessConceptIndex",
    "BusinessConceptSearchResult",
    "load_business_concept_index",
    "search_business_concept_index",
    "clear_business_concept_index_cache",
    "business_concept_index_cache_size",
    "SOURCE_OBJECT_CORE_ALIAS",
    "SOURCE_OBJECT_CORE_BUSINESS_NAME",
    "SOURCE_OBJECT_CORE_ENTITY_CONCEPT",
    "SOURCE_VIEW_ENRICHED_ALIAS",
    "SOURCE_VIEW_ENRICHED_DOMAIN",
    "SOURCE_VIEW_ENRICHED_STATUS_CONCEPT",
    "SOURCE_ENTITY_ASSIGNMENT",
    "SOURCE_DOMAIN_ASSIGNMENT",
    "SOURCE_SEMANTIC_CONTRACT_ENTITY_NAME",
    "SOURCE_SEMANTIC_CONTRACT_SYNONYM",
    "SOURCE_SEMANTIC_CONTRACT_ATTRIBUTE",
]

# ── Provenance source labels (requirement 4 — every match keeps its origin) ─

SOURCE_OBJECT_CORE_ALIAS = "OBJECT_CORE_ALIAS"
SOURCE_OBJECT_CORE_BUSINESS_NAME = "OBJECT_CORE_BUSINESS_NAME"
SOURCE_OBJECT_CORE_ENTITY_CONCEPT = "OBJECT_CORE_ENTITY_CONCEPT"
SOURCE_VIEW_ENRICHED_ALIAS = "VIEW_ENRICHED_ALIAS"
SOURCE_VIEW_ENRICHED_DOMAIN = "VIEW_ENRICHED_DOMAIN"
SOURCE_VIEW_ENRICHED_STATUS_CONCEPT = "VIEW_ENRICHED_STATUS_CONCEPT"
SOURCE_ENTITY_ASSIGNMENT = "ENTITY_ASSIGNMENT"
SOURCE_DOMAIN_ASSIGNMENT = "DOMAIN_ASSIGNMENT"
SOURCE_SEMANTIC_CONTRACT_ENTITY_NAME = "SEMANTIC_CONTRACT_ENTITY_NAME"
SOURCE_SEMANTIC_CONTRACT_SYNONYM = "SEMANTIC_CONTRACT_SYNONYM"
SOURCE_SEMANTIC_CONTRACT_ATTRIBUTE = "SEMANTIC_CONTRACT_ATTRIBUTE"

_UNTRUSTED_ASSIGNMENT_SOURCES = frozenset({"preliminary_connect_time"})
_UNTRUSTED_LABELS = frozenset({"", "unknown", "none", "null"})

_MAX_MATCHED_EVIDENCE_PER_RESULT = 10
_MAX_CACHE_ENTRIES = 16


@dataclass(frozen=True)
class MatchedConceptEvidence:
    """One provenance-carrying justification for a business-concept match.
    `concept` is the exact matched term (e.g. "program"); `source` is one of
    the SOURCE_* constants above; `matched_field` is the
    core.sqlservice.lexical_index FIELD_* name the term was folded into;
    `column_name` is set only for SOURCE_SEMANTIC_CONTRACT_ATTRIBUTE
    evidence (the real column this concept maps to)."""

    concept: str
    source: str
    matched_field: str
    column_name: str | None = None


@dataclass(frozen=True)
class BusinessConceptIndexIdentity:
    """Exact cache identity. `enrichment_state` is an opaque, comparison-
    only tuple (see _enrichment_state) — two identities differing only in
    that field are never considered the same index."""

    build_id: int
    source_id: int
    enrichment_state: tuple


@dataclass(frozen=True)
class BusinessConceptIndex:
    """An immutable, ready-to-search business-concept index. `lexical_index`
    carries the BM25F-ready term statistics; `evidence_by_object_and_token`
    maps object_fqn (lowercased) -> token -> the bounded, deduped provenance
    that produced it, for post-search attribution (requirement 4/10)."""

    identity: BusinessConceptIndexIdentity
    lexical_index: LexicalIndex
    evidence_by_object_and_token: Mapping[str, Mapping[str, tuple[MatchedConceptEvidence, ...]]]


@dataclass(frozen=True)
class BusinessConceptSearchResult:
    """One ranked hit from search_business_concept_index()."""

    object_fqn: str
    score: float
    matched_fields: tuple[str, ...]
    matched_evidence: tuple[MatchedConceptEvidence, ...]


@dataclass(frozen=True)
class _ConceptDocument:
    """Duck-typed input to core.sqlservice.lexical_index.build_lexical_index
    (which only ever reads object_fqn/object_type/retrieval_tier/
    document_hash/document_text/metadata) — never rendered/shown to a user
    or an AI provider, so document_text is always empty."""

    object_fqn: str
    object_type: str
    retrieval_tier: int
    document_hash: str
    document_text: str
    metadata: Mapping[str, object]


_cache: "OrderedDict[tuple, BusinessConceptIndex]" = OrderedDict()
_cache_lock = RLock()


def clear_business_concept_index_cache() -> None:
    with _cache_lock:
        _cache.clear()


def business_concept_index_cache_size() -> int:
    with _cache_lock:
        return len(_cache)


# ── Enrichment-state fingerprint (cache invalidation only) ────────────────


def _table_state(conn, sql: str, params: tuple) -> tuple[int, str]:
    try:
        row = conn.execute(sql, params).fetchone()
        return (int(row["c"] or 0), str(row["m"] or ""))
    except Exception:  # noqa: BLE001 — a missing/not-yet-migrated table must never break retrieval
        return (0, "")


def _enrichment_state(source_id: int) -> tuple:
    """Cheap, indexed (count, latest updated_at) signature per trust source
    — comparison-only, never treated as semantic evidence itself. Fails
    open per-table (see _table_state) so a partially-migrated/empty
    database still produces a stable, cacheable (if all-zero) identity."""
    conn = get_connection()
    try:
        return (
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM sql_semantic_enrichments WHERE source_id = ? AND phase = ? AND status = ?",
                (source_id, PHASE_OBJECT_CORE, STATUS_ACTIVE),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM sql_view_semantic_enrichments WHERE source_id = ? AND status = ?",
                (source_id, STATUS_ACTIVE),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM entity_assignments WHERE source_id = ?",
                (source_id,),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM domain_assignments WHERE source_id = ?",
                (source_id,),
            ),
            _table_state(
                conn,
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
                "FROM semantic_entity_contracts WHERE source_id = ? AND resolution_status = 'RESOLVED'",
                (source_id,),
            ),
        )
    finally:
        conn.close()


# ── Trust-source readers (read-only, fail open) ────────────────────────────


def _object_core_terms_by_fqn_lower(
    source_id: int, user_id: str, documents: Sequence,
) -> dict[str, list[tuple[str, str, str]]]:
    """object_fqn_lower -> [(term, source_label, field), ...] from current,
    ACTIVE OBJECT_CORE rows only. Fails open to {} on any error."""
    try:
        provider = OpenAISemanticEnrichmentProvider()
        provider_id, model = provider.provider_id, provider.model
        obj_inputs, _skipped = build_object_semantic_inputs(documents)
        if not obj_inputs:
            return {}
        current_hashes = {
            obj.object_fqn: compute_object_core_cache_key(obj, provider_id, model).semantic_input_hash
            for obj in obj_inputs
        }
        stored = load_current_enrichments_for_source(
            source_id, user_id, provider_id=provider_id, model=model,
            contract_version=SEMANTIC_ENRICHMENT_CONTRACT_VERSION,
            current_hashes_by_object_fqn=current_hashes, phase=PHASE_OBJECT_CORE,
        )
    except Exception:  # noqa: BLE001 — fail open; this lane is a bonus signal, never a hard dependency
        return {}

    out: dict[str, list[tuple[str, str, str]]] = {}
    for row in stored:
        key = row.object_fqn.lower()
        terms = out.setdefault(key, [])
        if row.business_name:
            terms.append((row.business_name, SOURCE_OBJECT_CORE_BUSINESS_NAME, "business_name"))
        for alias in row.aliases:
            if alias and alias.strip():
                terms.append((alias, SOURCE_OBJECT_CORE_ALIAS, "synonyms"))
        for concept in row.entity_concepts:
            if concept and concept.strip():
                terms.append((concept, SOURCE_OBJECT_CORE_ENTITY_CONCEPT, "synonyms"))
    return out


def _view_enriched_terms_by_fqn_lower(
    source_id: int, user_id: str, documents: Sequence,
) -> dict[str, list[tuple[str, str, str]]]:
    """object_fqn_lower -> [(term, source_label, field), ...] from current,
    ACTIVE VIEW_ENRICHED rows only (Tier 1/2 views — the same eligibility
    core.sqlservice.view_semantic_enrichment_preparation.
    build_eligible_view_inputs already enforces). Fails open to {}."""
    try:
        view_inputs = build_eligible_view_inputs(source_id, documents)
        if not view_inputs:
            return {}
        provider = OpenAISemanticEnrichmentProvider()
        current_hashes = {
            vi.object_fqn: compute_view_cache_key(vi, provider.provider_id, provider.model).semantic_input_hash
            for vi in view_inputs
        }
        stored = load_current_view_enrichments_for_source(
            source_id, user_id, provider_id=provider.provider_id, model=provider.model,
            contract_version=VIEW_ENRICHMENT_CONTRACT_VERSION, current_hashes_by_object_fqn=current_hashes,
        )
    except Exception:  # noqa: BLE001 — fail open; this lane is a bonus signal, never a hard dependency
        return {}

    out: dict[str, list[tuple[str, str, str]]] = {}
    for row in stored:
        key = row.object_fqn.lower()
        terms = out.setdefault(key, [])
        if row.business_name:
            terms.append((row.business_name, SOURCE_OBJECT_CORE_BUSINESS_NAME, "business_name"))
        if row.business_domain and row.business_domain.strip():
            terms.append((row.business_domain, SOURCE_VIEW_ENRICHED_DOMAIN, "synonyms"))
        for alias in row.aliases:
            if alias and alias.strip():
                terms.append((alias, SOURCE_VIEW_ENRICHED_ALIAS, "synonyms"))
        for status_concept in row.status_concepts:
            label = getattr(status_concept, "label", None)
            if label and str(label).strip():
                terms.append((str(label), SOURCE_VIEW_ENRICHED_STATUS_CONCEPT, "column_names"))
    return out


def _entity_domain_terms_by_fqn_lower(
    source_id: int, user_id: str,
) -> dict[str, list[tuple[str, str, str]]]:
    """object_fqn_lower -> [(term, source_label, field), ...] from
    entity_assignments/domain_assignments — excludes "Unknown"/blank labels
    and preliminary_connect_time provenance (see module docstring). Fails
    open to {}."""
    out: dict[str, list[tuple[str, str, str]]] = {}
    try:
        for row in list_entity_assignments(source_id, user_id) or ():
            label = str(row.get("entity") or "").strip()
            if not label or label.lower() in _UNTRUSTED_LABELS:
                continue
            if row.get("assignment_source") in _UNTRUSTED_ASSIGNMENT_SOURCES:
                continue
            key = str(row.get("table_fqn") or "").lower()
            if not key:
                continue
            out.setdefault(key, []).append((label, SOURCE_ENTITY_ASSIGNMENT, "synonyms"))
    except Exception:  # noqa: BLE001 — fail open
        pass
    try:
        for row in list_domain_assignments(source_id, user_id) or ():
            label = str(row.get("domain") or "").strip()
            if not label or label.lower() in _UNTRUSTED_LABELS:
                continue
            if row.get("assignment_source") in _UNTRUSTED_ASSIGNMENT_SOURCES:
                continue
            key = str(row.get("table_fqn") or "").lower()
            if not key:
                continue
            out.setdefault(key, []).append((label, SOURCE_DOMAIN_ASSIGNMENT, "synonyms"))
    except Exception:  # noqa: BLE001 — fail open
        pass
    return out


def _distinct_entity_names(source_id: int, user_id: str) -> tuple[str, ...]:
    """Generic discovery of candidate entity names for this source — reads
    ONLY the distinct labels entity_assignments already recorded for THIS
    source (never a fixed/hardcoded vocabulary), so
    get_latest_entity_contract can be asked, per name, whether a RESOLVED
    contract exists. Fails open to ()."""
    try:
        rows = list_entity_assignments(source_id, user_id) or ()
        names = {
            str(row.get("entity") or "").strip()
            for row in rows
            if str(row.get("entity") or "").strip().lower() not in _UNTRUSTED_LABELS
        }
        return tuple(sorted(names))
    except Exception:  # noqa: BLE001 — fail open
        return ()


def _semantic_contract_terms_by_fqn_lower(
    source_id: int, user_id: str, real_columns_by_fqn_lower: Mapping[str, frozenset[str]],
) -> dict[str, list[tuple[str, str, str, str | None]]]:
    """object_fqn_lower -> [(term, source_label, field, column_name), ...]
    from only the LATEST, RESOLVED semantic_entity_contracts per entity
    name. A contract's own claimed attribute column is kept only when that
    column genuinely exists (case-insensitively) on the real object per
    THIS build's own schema metadata — a stale/wrong claim about a column
    that no longer exists is dropped rather than trusted (requirement 5:
    "semantic claims referencing nonexistent columns" are excluded). Fails
    open to {}."""
    out: dict[str, list[tuple[str, str, str, str | None]]] = {}
    for entity_name in _distinct_entity_names(source_id, user_id):
        try:
            record = get_latest_entity_contract(source_id, entity_name)
        except Exception:  # noqa: BLE001 — fail open per entity, never abort the whole pass
            continue
        if not record or record.get("resolution_status") != "RESOLVED":
            continue
        contract = record.get("contract") or {}
        table_fqn = (
            (contract.get("primary_key") or {}).get("table_fqn")
            or contract.get("canonical_table_fqn")
            or record.get("canonical_table_fqn")
        )
        if not table_fqn or not isinstance(table_fqn, str):
            continue
        key = table_fqn.lower()
        real_columns = real_columns_by_fqn_lower.get(key)
        if real_columns is None:
            continue  # the contract's object is not part of this build's current schema at all
        terms = out.setdefault(key, [])
        if entity_name:
            terms.append((entity_name, SOURCE_SEMANTIC_CONTRACT_ENTITY_NAME, "synonyms", None))
        for synonym in contract.get("synonyms") or ():
            if isinstance(synonym, str) and synonym.strip():
                terms.append((synonym, SOURCE_SEMANTIC_CONTRACT_SYNONYM, "synonyms", None))
        for attribute in contract.get("attributes") or ():
            if not isinstance(attribute, dict):
                continue
            business_name = attribute.get("business_name")
            column_name = attribute.get("column_name")
            if not isinstance(business_name, str) or not business_name.strip():
                continue
            if not isinstance(column_name, str) or column_name.lower() not in real_columns:
                continue  # stale/nonexistent-column claim — never trusted
            terms.append((business_name, SOURCE_SEMANTIC_CONTRACT_ATTRIBUTE, "column_names", column_name))
    return out


# ── Index construction (pure given already-fetched term maps) ─────────────


def _assemble_index(
    build_id: int,
    source_id: int,
    user_id: str,
    documents: Sequence,
    allowed_tiers: frozenset[int],
    enrichment_state: tuple,
) -> BusinessConceptIndex:
    eligible = [d for d in documents if d.retrieval_tier in allowed_tiers]
    real_columns_by_fqn_lower = {
        d.object_fqn.lower(): frozenset(str(c).lower() for c in (d.metadata.get("column_names") or ()))
        for d in eligible
    }

    combined: dict[str, list[tuple[str, str, str, str | None]]] = {}

    def _extend(source_map: Mapping[str, Sequence[tuple]]) -> None:
        for key, terms in source_map.items():
            if key not in real_columns_by_fqn_lower:
                continue  # not a current, tier-eligible object in THIS build — never trusted
            bucket = combined.setdefault(key, [])
            for term in terms:
                bucket.append(term if len(term) == 4 else (*term, None))

    _extend(_object_core_terms_by_fqn_lower(source_id, user_id, eligible))
    _extend(_view_enriched_terms_by_fqn_lower(source_id, user_id, eligible))
    _extend(_entity_domain_terms_by_fqn_lower(source_id, user_id))
    _extend(_semantic_contract_terms_by_fqn_lower(source_id, user_id, real_columns_by_fqn_lower))

    doc_by_fqn_lower = {d.object_fqn.lower(): d for d in eligible}
    concept_documents: list[_ConceptDocument] = []
    evidence_by_object_and_token: dict[str, dict[str, list[MatchedConceptEvidence]]] = {}

    for key, real_doc in doc_by_fqn_lower.items():
        terms = combined.get(key, ())
        business_names: list[str] = []
        synonyms: list[str] = []
        column_names: list[str] = []
        token_evidence: dict[str, list[MatchedConceptEvidence]] = {}

        for term_text, source_label, field, column_name in terms:
            if field == "business_name":
                business_names.append(term_text)
            elif field == "synonyms":
                synonyms.append(term_text)
            elif field == "column_names":
                column_names.append(term_text)
            for token in tokenize(term_text):
                token_evidence.setdefault(token, []).append(
                    MatchedConceptEvidence(
                        concept=term_text, source=source_label, matched_field=field.upper(),
                        column_name=column_name,
                    )
                )

        evidence_by_object_and_token[key] = {
            token: tuple(items[:_MAX_MATCHED_EVIDENCE_PER_RESULT]) for token, items in token_evidence.items()
        }

        concept_documents.append(
            _ConceptDocument(
                object_fqn=real_doc.object_fqn,
                object_type=real_doc.object_type,
                retrieval_tier=real_doc.retrieval_tier,
                document_hash=real_doc.document_hash,
                document_text="",
                metadata={
                    "business_name": business_names[0] if business_names else "",
                    "description": "",
                    "synonyms": tuple(synonyms),
                    "column_names": tuple(column_names),
                },
            )
        )

    identity = LexicalIndexIdentity(
        build_id=build_id, embedding_provider="business_concept", embedding_model="v1", embedding_dimensions=0,
    )
    lexical_index = build_lexical_index(concept_documents, identity)

    return BusinessConceptIndex(
        identity=BusinessConceptIndexIdentity(
            build_id=build_id, source_id=source_id, enrichment_state=enrichment_state,
        ),
        lexical_index=lexical_index,
        evidence_by_object_and_token=evidence_by_object_and_token,
    )


def _empty_index(build_id: int, source_id: int, enrichment_state: tuple) -> BusinessConceptIndex:
    identity = LexicalIndexIdentity(
        build_id=build_id, embedding_provider="business_concept", embedding_model="v1", embedding_dimensions=0,
    )
    return BusinessConceptIndex(
        identity=BusinessConceptIndexIdentity(build_id=build_id, source_id=source_id, enrichment_state=enrichment_state),
        lexical_index=build_lexical_index((), identity),
        evidence_by_object_and_token={},
    )


# ── Public entry points ─────────────────────────────────────────────────


def load_business_concept_index(
    build_id: int,
    source_id: int,
    user_id: str,
    documents: Sequence,
    *,
    allowed_tiers: frozenset[int],
    use_cache: bool = True,
) -> BusinessConceptIndex:
    """
    Load (building and caching if necessary) the business-concept index for
    one retrieval build. Never raises: any failure while reading a trust
    source degrades that ONE source to "no evidence" (see each _*_terms_by_
    fqn_lower helper's own fail-open contract) and a catastrophic failure
    anywhere in assembly degrades the WHOLE index to empty (zero matches —
    identical to "vector/lexical behavior unchanged when no semantic index
    exists", requirement satisfied by construction, never a special case
    the caller must handle).
    """
    try:
        enrichment_state = _enrichment_state(source_id)
    except Exception:  # noqa: BLE001
        enrichment_state = ()

    key = (build_id, source_id, enrichment_state)
    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                _cache.move_to_end(key)
                return cached

    try:
        index = _assemble_index(build_id, source_id, user_id, documents, allowed_tiers, enrichment_state)
    except Exception:  # noqa: BLE001 — this lane is additive; a bug here must never break retrieval
        index = _empty_index(build_id, source_id, enrichment_state)

    if use_cache:
        with _cache_lock:
            _cache[key] = index
            while len(_cache) > _MAX_CACHE_ENTRIES:
                _cache.popitem(last=False)
    return index


def search_business_concept_index(
    index: BusinessConceptIndex, query_tokens: Sequence[str], *, top_k: int,
) -> tuple[BusinessConceptSearchResult, ...]:
    """Search the given index — pure, in-memory, no I/O. A query with zero
    matching tokens in any field returns an empty tuple (requirement 9: an
    object must have actual matched evidence to receive a score)."""
    if not query_tokens or index.lexical_index.document_count == 0:
        return ()
    hits = search_lexical_index(index.lexical_index, query_tokens, top_k=top_k)
    results = []
    for hit in hits:
        key = hit.object_fqn.lower()
        per_token = index.evidence_by_object_and_token.get(key, {})
        evidence: list[MatchedConceptEvidence] = []
        seen: set[tuple[str, str, str | None]] = set()
        for token in hit.matched_tokens:
            for item in per_token.get(token, ()):
                dedup_key = (item.concept.lower(), item.source, item.column_name)
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                evidence.append(item)
                if len(evidence) >= _MAX_MATCHED_EVIDENCE_PER_RESULT:
                    break
            if len(evidence) >= _MAX_MATCHED_EVIDENCE_PER_RESULT:
                break
        results.append(
            BusinessConceptSearchResult(
                object_fqn=hit.object_fqn, score=hit.score,
                matched_fields=hit.matched_fields, matched_evidence=tuple(evidence),
            )
        )
    return tuple(results)
