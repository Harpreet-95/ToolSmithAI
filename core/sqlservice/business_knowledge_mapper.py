"""
Automatic business-knowledge-to-schema mapping -- Phase 7, Step 3J, Part 4.

Given a source's stored business-knowledge package (core.sqlservice.
business_knowledge_mapping providers/data.business_knowledge_package_service)
and its current READY retrieval build, proposes AI mappings from package
concepts to real schema objects, then deterministically verifies every
proposal against facts this codebase already persists -- never trusting the
AI response on its own:

  - object existence: object_fqn must be a REAL object in this build's own
    indexed document set (core.sqlservice.vector_index.load_vector_index) --
    a hallucinated FQN is rejected outright, never silently dropped from
    the response but kept as "verified".
  - tier eligibility: only Tier 1/2 objects (the SAME eligible-tier set
    core.sqlservice.schema_frontier uses) can ever become a VERIFIED
    mapping -- Tier 3/4/archive/system objects are always REJECTED.
  - column existence: every proposed column must be a REAL column on that
    object (from the document's own metadata) -- a hallucinated column is
    dropped from the mapping's column list (never trusted), and if every
    proposed column for a mapping was hallucinated, that mapping's kind is
    downgraded and its evidence records the discrepancy.
  - authority/safety: an object core.sqlservice.schema_object_authority has
    already classified AUTHORITY_CLASS_UNSAFE_DEPENDENCY is always
    REJECTED, regardless of AI confidence.
  - source isolation: structural by construction -- this module only ever
    sees `source_id`'s own build documents, never another source's.
  - confidence floor: even a mapping that passes every structural check
    above is only auto-promoted to VERIFIED when its reported confidence
    meets core.config.BUSINESS_KNOWLEDGE_MAPPING_MIN_VERIFIED_CONFIDENCE;
    below that, it is persisted as UNRESOLVED (kept, not silently dropped,
    and never presented as verified).

Bounded batches: concepts are mapped `BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE`
at a time, each call given a lexically-bounded candidate object roster (never
the whole schema) built with core.sqlservice.lexical_index's own tokenizer --
no new embedding call, no ranking dependency on the question-time pipeline.

Idempotent + resumable, at two granularities:
  - whole-package fast path: a rerun against a totally unchanged package and
    schema first checks data.business_knowledge_mapping_store.
    get_mapping_run for a COMPLETE row at the exact (schema_fingerprint,
    compute_package_content_hash(...)) pair and returns immediately with
    zero provider calls.
  - per-concept resumability: every mapping row is tagged with its OWN
    concept's content hash (_concept_content_hash), not the whole-package
    hash -- so editing or adding ONE concept only triggers a provider call
    for that concept; every other, unchanged concept's existing mapping
    (matched via data.business_knowledge_mapping_store.
    get_concept_content_hashes_by_schema_fingerprint) is left untouched and
    never re-proposed. This also means a crash mid-run never re-pays for
    already-completed concepts on retry.

No source-specific business constants live in this module -- concept ids,
display names, and mapping content all come from the caller's stored
package (data.business_knowledge_package_service), never hardcoded here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from core.config import (
    BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE,
    BUSINESS_KNOWLEDGE_MAPPING_MAX_OUTPUT_TOKENS,
    BUSINESS_KNOWLEDGE_MAPPING_MIN_VERIFIED_CONFIDENCE,
)
from core.sqlservice.business_knowledge_mapping_providers import (
    BusinessKnowledgeMappingProvider,
    BusinessKnowledgeMappingProviderError,
    OpenAIBusinessKnowledgeMappingProvider,
)
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.lexical_index import tokenize
from core.sqlservice.retrieval_store import get_retrieval_build
from core.sqlservice.schema_object_authority import (
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW,
    AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
    AUTHORITY_CLASS_UNSAFE_DEPENDENCY,
    load_schema_object_authority,
)
from core.sqlservice.vector_index import STATUS_OK, load_vector_index

import data.business_knowledge_mapping_store as mapping_store
import data.business_knowledge_package_service as package_service

logger = logging.getLogger(__name__)

MAPPING_CONTRACT_VERSION = 1
_ELIGIBLE_TIERS = frozenset({1, 2})
_MAX_CANDIDATE_OBJECTS_PER_BATCH = 60
_MAX_COLUMNS_LISTED_PER_OBJECT = 25
# Step 3J1 fix -- a provider's own "evidence" strings are unvalidated free
# text and observed live to sometimes echo back a large fraction of the
# candidate description it was given (e.g. a 20+ column list), which can
# make core.sqlservice.schema_frontier's per-object character budget drop
# business_knowledge_mappings entirely for a wide object. Bounded here, at
# the point evidence is first produced, as well as (defensively, for
# already-stored rows) in core.sqlservice.business_knowledge_evidence.
_MAX_EVIDENCE_STRINGS_PER_MAPPING = 3
_MAX_EVIDENCE_STRING_LENGTH = 120


def _bounded_evidence(raw_evidence: "tuple[str, ...]") -> "tuple[str, ...]":
    bounded: list[str] = []
    for item in raw_evidence[:_MAX_EVIDENCE_STRINGS_PER_MAPPING]:
        text = str(item)
        if len(text) > _MAX_EVIDENCE_STRING_LENGTH:
            text = text[:_MAX_EVIDENCE_STRING_LENGTH].rstrip() + "..."
        bounded.append(text)
    return tuple(bounded)


# ---------------------------------------------------------------------------
# Phase 7, Step 3J2, Part 3 -- generic verification hardening. Neither rule
# below ever inspects an object/concept NAME; both operate purely on
# concept_type, mapping_role, object_type, authority evidence, and the
# package's own relationship graph, so they apply identically to any
# source's package, not just one customer's.
# ---------------------------------------------------------------------------

# A view-preferring role is only ever trustworthy when core.sqlservice.
# schema_object_authority has ALREADY structurally proven the object a
# declared-preferred or proven-equivalent view for some entity -- AI
# confidence alone, or a bare VIEW/TABLE type match, is never sufficient.
# Requiring DECLARED_PREFERRED_VIEW/PROVEN_EQUIVALENT_VIEW here also means a
# genuinely owner-confirmed "preferred view" claim (which does not go
# through this AI-verification path at all -- see data.business_knowledge_
# package_service/scripts' own direct-write convention) is unaffected.
_PREFERRED_VIEW_ROLE = "PREFERRED_BUSINESS_VIEW"
_AUTHORITY_CLASSES_ELIGIBLE_FOR_PREFERRED_VIEW = frozenset({
    AUTHORITY_CLASS_DECLARED_PREFERRED_VIEW, AUTHORITY_CLASS_PROVEN_EQUIVALENT_VIEW,
})

# Roles that represent a "what this object charges/what was paid" fact for a
# PAYMENT_CONCEPT -- these are the roles Part 3's payment-family conflict
# check applies to. Never includes CANONICAL_ENTITY_SOURCE/STATUS_SOURCE/
# etc., which are not payment-meaning roles at all.
_PAYMENT_FAMILY_ROLES = frozenset({
    "FEE_SOURCE", "FULL_PAYMENT_SOURCE", "INSTALLMENT_PAYMENT_SOURCE",
    "FEE_DEFINITION_SOURCE", "FEE_PAYMENT_SOURCE",
})
_MAX_PROGRAM_FAMILY_HOPS = 3


def _compute_program_families(
    concepts: list[dict], relationships: list[dict],
) -> dict[str, frozenset[str]]:
    """concept_id -> the set of PROGRAM-type concept_ids reachable from it
    within _MAX_PROGRAM_FAMILY_HOPS hops of the package's own relationship
    graph (undirected -- "connected to program X" does not depend on which
    way the edge happens to point). A concept with no path to any PROGRAM
    concept within the bound gets an empty set, meaning "family unknown" --
    the conflict check below never fires for it (a concept unrelated to any
    program by the package's own evidence is not something this rule can
    safely reason about)."""
    concepts_by_id = {c["concept_id"]: c for c in concepts}
    program_ids = {cid for cid, c in concepts_by_id.items() if c["concept_type"] == "PROGRAM"}
    adjacency: dict[str, set[str]] = {}
    for rel in relationships:
        adjacency.setdefault(rel["from_concept_id"], set()).add(rel["to_concept_id"])
        adjacency.setdefault(rel["to_concept_id"], set()).add(rel["from_concept_id"])

    families: dict[str, frozenset[str]] = {}
    for concept_id in concepts_by_id:
        if concept_id in program_ids:
            continue
        visited = {concept_id}
        frontier = {concept_id}
        reached_programs: set[str] = set()
        for _hop in range(_MAX_PROGRAM_FAMILY_HOPS):
            next_frontier: set[str] = set()
            for node in frontier:
                for neighbor in adjacency.get(node, ()):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    if neighbor in program_ids:
                        reached_programs.add(neighbor)
                    else:
                        next_frontier.add(neighbor)
            frontier = next_frontier
            if not frontier:
                break
        families[concept_id] = frozenset(reached_programs)
    return families

__all__ = [
    "MAPPING_CONTRACT_VERSION",
    "MappingRunReport",
    "compute_package_content_hash",
    "run_business_knowledge_mapping",
]


@dataclass(frozen=True)
class MappingRunReport:
    package_id: int
    schema_fingerprint: str
    content_hash: str
    reused: bool
    provider_calls_made: int
    batches_total: int
    batches_completed: int
    verified_count: int
    unresolved_count: int
    rejected_count: int
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    # A structurally-valid, deterministically-verified proposal that the
    # underlying storage layer itself refused (e.g. a mapping_role the
    # live database's own schema does not yet accept -- see
    # run_business_knowledge_mapping's own try/except around upsert_mapping
    # below). Never fatal to the run; counted here so it is visible rather
    # than silently swallowed. Zero for every ordinary run.
    storage_rejected_count: int = 0


def _concept_content_hash(concept: dict) -> str:
    """Per-CONCEPT content hash -- what actually gets stamped onto each
    mapping row's content_hash column (never the whole-package hash). This
    is what lets an unrelated edit elsewhere in the package (which changes
    compute_package_content_hash's own package-wide value) avoid forcing a
    re-proposal for every OTHER, unchanged concept -- only the package-wide
    hash changes; each concept's own hash is stable until IT changes."""
    canonical = {k: concept[k] for k in (
        "concept_id", "concept_type", "display_name", "description", "aliases", "confidence", "evidence",
    )}
    canonical_text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def compute_package_content_hash(package_id: int) -> str:
    """Canonical hash of every concept/relationship/fact currently in this
    package -- changes whenever the owner adds/edits anything, which is
    exactly the "package content" axis data.business_knowledge_mapping_
    store's (package_id, schema_fingerprint, content_hash) generation key
    is meant to invalidate on. Never includes id/created_at/updated_at
    (those are storage bookkeeping, not business content)."""
    concepts = package_service.list_concepts(package_id)
    relationships = package_service.list_relationships(package_id)
    facts = package_service.list_facts(package_id)

    def _strip(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
        return [{k: r[k] for k in keys} for r in rows]

    canonical = {
        "concepts": sorted(
            _strip(concepts, ("concept_id", "concept_type", "display_name", "description",
                              "aliases", "origin", "confidence", "evidence")),
            key=lambda r: r["concept_id"],
        ),
        "relationships": sorted(
            _strip(relationships, ("from_concept_id", "relationship_type", "to_concept_id",
                                    "origin", "confidence", "evidence")),
            key=lambda r: (r["from_concept_id"], r["relationship_type"], r["to_concept_id"]),
        ),
        "facts": sorted(
            _strip(facts, ("fact_key", "fact_text", "related_concept_id")),
            key=lambda r: r["fact_key"],
        ),
    }
    canonical_text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def _object_known_columns(doc) -> set[str]:
    metadata = doc.metadata
    known: set[str] = set()
    for key in ("column_names", "primary_key_columns", "measure_columns", "date_columns", "status_columns"):
        for name in metadata.get(key) or ():
            known.add(str(name).lower())
    return known


def _candidate_roster_text(doc, *, max_columns: int) -> str:
    metadata = doc.metadata
    columns = sorted(_object_known_columns(doc))[:max_columns]
    parts = [f"OBJECT: {doc.object_fqn}", f"TYPE: {doc.object_type}"]
    if metadata.get("business_name"):
        parts.append(f"BUSINESS_NAME: {metadata['business_name']}")
    if metadata.get("domain"):
        parts.append(f"DOMAIN: {metadata['domain']}")
    if columns:
        parts.append(f"COLUMNS: {', '.join(columns)}")
    return " | ".join(parts)


def _rank_candidate_objects(documents, concept: dict, *, limit: int) -> list:
    """Bounded, deterministic lexical-overlap ranking of this build's own
    Tier 1/2 documents against one concept's own display_name/aliases/
    description -- no embedding call, no dependency on the question-time
    ranking pipeline. Ties break on object_fqn for determinism."""
    concept_tokens: set[str] = set()
    for text in (concept.get("display_name"), concept.get("description")):
        if text:
            concept_tokens.update(tokenize(text))
    for alias in concept.get("aliases") or ():
        concept_tokens.update(tokenize(alias))
    concept_tokens.add(concept["concept_id"].lower())

    scored = []
    for doc in documents:
        if doc.retrieval_tier not in _ELIGIBLE_TIERS:
            continue
        doc_tokens: set[str] = set(tokenize(doc.object_fqn))
        if doc.metadata.get("business_name"):
            doc_tokens.update(tokenize(doc.metadata["business_name"]))
        doc_tokens.update(_object_known_columns(doc))
        overlap = len(concept_tokens & doc_tokens)
        scored.append((overlap, doc))
    scored.sort(key=lambda t: (-t[0], t[1].object_fqn.lower()))
    return [doc for _score, doc in scored[:limit]]


def _build_prompt(concepts_batch: list[dict], candidate_docs_by_concept: dict[str, list]) -> tuple[str, str]:
    system_prompt = (
        "You map business concepts from a database owner's business-knowledge package onto real "
        "database schema objects. You are given, for each concept, a bounded list of candidate "
        "schema objects with their real names and columns. You must ONLY reference object names and "
        "column names that literally appear in the candidate list you were given -- never invent an "
        "object or column name. For each concept, propose zero or more mappings, each naming exactly "
        "one candidate object, a mapping_role describing what that object is FOR this concept, a "
        "mapping_kind describing how directly it satisfies the concept, a confidence between 0 and 1, "
        "and short evidence strings citing what in the candidate list justified the mapping. If no "
        "candidate is a good fit for a concept, propose nothing for it."
    )
    lines: list[str] = []
    for concept in concepts_batch:
        lines.append(
            f"CONCEPT: {concept['concept_id']} ({concept['concept_type']}) -- {concept['display_name']}"
        )
        if concept.get("description"):
            lines.append(f"  DESCRIPTION: {concept['description']}")
        if concept.get("aliases"):
            lines.append(f"  ALIASES: {', '.join(concept['aliases'])}")
        docs = candidate_docs_by_concept.get(concept["concept_id"], [])
        lines.append("  CANDIDATES:")
        for doc in docs:
            lines.append(f"    - {_candidate_roster_text(doc, max_columns=_MAX_COLUMNS_LISTED_PER_OBJECT)}")
    user_prompt = "\n".join(lines)
    return system_prompt, user_prompt


@dataclass(frozen=True)
class _VerifiedProposal:
    concept_id: str
    object_fqn: str
    mapping_role: str
    mapping_kind: str
    status: str
    confidence: float
    columns: tuple[str, ...]
    evidence: tuple[str, ...]
    rejection_reason: str | None


def _verify_proposal(
    raw: dict, *, known_concept_ids: set[str], documents_by_fqn_lower: dict,
    authority_by_fqn_lower: dict, min_verified_confidence: float,
    concepts_by_id: dict, program_family_by_concept_id: dict, package_id: int,
) -> _VerifiedProposal | None:
    """Returns None for a structurally malformed proposal (missing
    required field) -- silently discarded, never persisted at all (it names
    nothing real to reject against). Otherwise always returns a
    _VerifiedProposal with a definite status: VERIFIED, UNRESOLVED, or
    REJECTED (with rejection_reason set)."""
    try:
        concept_id = str(raw["concept_id"])
        object_fqn = str(raw["object_fqn"])
        mapping_role = str(raw["mapping_role"])
        mapping_kind = str(raw["mapping_kind"])
        confidence = float(raw["confidence"])
        proposed_columns = [str(c) for c in (raw.get("columns") or [])]
        evidence = tuple(str(e) for e in (raw.get("evidence") or []))
    except (KeyError, TypeError, ValueError):
        return None

    if concept_id not in known_concept_ids:
        return _VerifiedProposal(
            concept_id=concept_id, object_fqn=object_fqn, mapping_role=mapping_role,
            mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
            columns=(), evidence=evidence, rejection_reason="unknown_concept",
        )

    if mapping_role not in mapping_store.MAPPING_ROLES:
        return _VerifiedProposal(
            concept_id=concept_id, object_fqn=object_fqn, mapping_role="UNSUPPORTED_OR_CONFLICTING",
            mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
            columns=(), evidence=evidence, rejection_reason="invalid_mapping_role",
        )

    doc = documents_by_fqn_lower.get(object_fqn.lower())
    if doc is None:
        return _VerifiedProposal(
            concept_id=concept_id, object_fqn=object_fqn, mapping_role=mapping_role,
            mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
            columns=(), evidence=evidence, rejection_reason="object_not_found",
        )

    if doc.retrieval_tier not in _ELIGIBLE_TIERS:
        return _VerifiedProposal(
            concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
            mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
            columns=(), evidence=evidence, rejection_reason="ineligible_tier",
        )

    authority = authority_by_fqn_lower.get(object_fqn.lower())
    if authority is not None and authority.authority_class == AUTHORITY_CLASS_UNSAFE_DEPENDENCY:
        return _VerifiedProposal(
            concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
            mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
            columns=(), evidence=evidence, rejection_reason="unsafe_dependency",
        )

    # Step 3J2, Part 3, Rule A -- PREFERRED_BUSINESS_VIEW requires an actual
    # VIEW object AND authority evidence this codebase has already
    # structurally proven (declared-preferred or proven-equivalent) -- never
    # granted on AI confidence alone, and never on a physical TABLE. Checked
    # unconditionally, before the confidence threshold below, so a
    # high-confidence proposal cannot bypass it.
    if mapping_role == _PREFERRED_VIEW_ROLE:
        if str(doc.object_type).upper() != "VIEW":
            return _VerifiedProposal(
                concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
                mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
                columns=(), evidence=evidence, rejection_reason="preferred_view_requires_view_object",
            )
        if authority is None or authority.authority_class not in _AUTHORITY_CLASSES_ELIGIBLE_FOR_PREFERRED_VIEW:
            return _VerifiedProposal(
                concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
                mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
                columns=(), evidence=evidence,
                rejection_reason="preferred_view_requires_declared_or_proven_authority",
            )

    # Step 3J2, Part 3, Rule B -- a PAYMENT_CONCEPT mapping must not
    # conflict with an already-VERIFIED PAYMENT_CONCEPT mapping for the SAME
    # object when the package's own relationship graph places the two
    # concepts under verifiably DIFFERENT PROGRAM families. Never derived
    # from object or concept NAMES -- only concept_type + relationship
    # structure. A concept with no resolvable program family (empty set) is
    # never treated as conflicting with anything -- "unknown" is not
    # "different".
    if (
        concepts_by_id.get(concept_id, {}).get("concept_type") == "PAYMENT_CONCEPT"
        and mapping_role in _PAYMENT_FAMILY_ROLES
    ):
        this_family = program_family_by_concept_id.get(concept_id, frozenset())
        if this_family:
            for existing in mapping_store.list_mappings(package_id, status="VERIFIED", object_fqn=doc.object_fqn):
                if existing["concept_id"] == concept_id or existing["mapping_role"] not in _PAYMENT_FAMILY_ROLES:
                    continue
                other_concept = concepts_by_id.get(existing["concept_id"])
                if other_concept is None or other_concept.get("concept_type") != "PAYMENT_CONCEPT":
                    continue
                other_family = program_family_by_concept_id.get(existing["concept_id"], frozenset())
                if other_family and this_family.isdisjoint(other_family):
                    return _VerifiedProposal(
                        concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
                        mapping_kind="UNRESOLVED", status="REJECTED", confidence=confidence,
                        columns=(),
                        evidence=evidence + (
                            f"conflicts with existing VERIFIED mapping of this object to "
                            f"concept {existing['concept_id']!r} (disjoint program family per "
                            "package relationships)",
                        ),
                        rejection_reason="payment_family_conflict",
                    )

    known_columns = _object_known_columns(doc)
    verified_columns = tuple(c for c in proposed_columns if c.lower() in known_columns)
    hallucinated_columns = [c for c in proposed_columns if c.lower() not in known_columns]
    effective_evidence = evidence
    if hallucinated_columns:
        effective_evidence = effective_evidence + (
            f"discarded non-existent column(s): {', '.join(sorted(hallucinated_columns))}",
        )
    # Part 6 requirement: "views are evaluated by authority, restriction,
    # grain, and dependencies -- not blindly preferred". A restricted/
    # non-base-row object is never auto-rejected here (a filtered view can
    # legitimately be the right object for a narrower concept), but the
    # restriction/grain fact is always recorded in the mapping's own
    # evidence so a human reviewing UNRESOLVED/VERIFIED mappings -- and the
    # question-time selector reading this mapping's evidence via core.
    # sqlservice.business_knowledge_evidence -- can see it, never silently.
    if authority is not None:
        if authority.restriction_status not in ("UNKNOWN", "NONE"):
            effective_evidence = effective_evidence + (f"object restriction_status={authority.restriction_status}",)
        if authority.grain_class not in ("UNKNOWN", "BASE_ROW"):
            effective_evidence = effective_evidence + (f"object grain_class={authority.grain_class}",)

    if mapping_kind not in mapping_store.MAPPING_KINDS:
        mapping_kind = "UNRESOLVED"

    status = "VERIFIED" if confidence >= min_verified_confidence else "UNRESOLVED"

    return _VerifiedProposal(
        concept_id=concept_id, object_fqn=doc.object_fqn, mapping_role=mapping_role,
        mapping_kind=mapping_kind, status=status, confidence=confidence,
        columns=verified_columns, evidence=_bounded_evidence(effective_evidence), rejection_reason=None,
    )


def run_business_knowledge_mapping(
    source_id: int, user_id: str, build_id: int, embedding_provider: EmbeddingProvider,
    mapping_provider: BusinessKnowledgeMappingProvider | None = None,
    *, batch_size: int | None = None, force_concept_ids: frozenset[str] | None = None,
) -> MappingRunReport | None:
    """
    Runs (or resumes, or reuses) automatic mapping for source_id's ACTIVE
    business-knowledge package against build_id's current schema.

    Returns None if source_id has no ACTIVE package for user_id (nothing to
    map -- callers should treat this exactly like "no package", never as an
    error).

    load_vector_index() here costs ZERO new embedding calls (it only reads
    the already-persisted index for this build); the only provider calls
    this function ever makes are propose_mappings() calls, bounded to
    batch_size concepts each, and only for concepts that do not already
    have a mapping tagged with this exact (schema_fingerprint, content_hash)
    generation.

    force_concept_ids: Phase 7, Step 3J1 -- an explicit, named set of
    concept_ids to re-propose regardless of whether their own per-concept
    content hash already has a mapping under the current schema_fingerprint
    (e.g. retrying a concept whose only prior mappings were REJECTED, where
    a fresh provider call might legitimately propose a different, safe
    candidate this time). Every OTHER concept is still governed by the
    normal per-concept resumability check -- this never widens to "remap
    everything". Also disables the whole-package COMPLETE-run fast path
    below (which would otherwise short-circuit with zero provider calls
    before force_concept_ids is ever consulted, if nothing else about the
    package changed).
    """
    package = package_service.get_active_package(source_id, user_id)
    if package is None:
        return None

    build_row = get_retrieval_build(build_id, source_id, user_id)
    schema_fingerprint = build_row.schema_fingerprint if build_row is not None else None
    if not schema_fingerprint:
        return MappingRunReport(
            package_id=package["id"], schema_fingerprint="", content_hash="", reused=False,
            provider_calls_made=0, batches_total=0, batches_completed=0,
            verified_count=0, unresolved_count=0, rejected_count=0,
            error="no schema_fingerprint available for this build",
        )

    content_hash = compute_package_content_hash(package["id"])

    existing_run = mapping_store.get_mapping_run(package["id"], schema_fingerprint, content_hash)
    if existing_run is not None and existing_run["status"] == "COMPLETE" and not force_concept_ids:
        all_mappings = mapping_store.list_mappings(package["id"])
        return MappingRunReport(
            package_id=package["id"], schema_fingerprint=schema_fingerprint, content_hash=content_hash,
            reused=True, provider_calls_made=0,
            batches_total=existing_run["batches_total"], batches_completed=existing_run["batches_completed"],
            verified_count=sum(1 for m in all_mappings if m["status"] == "VERIFIED"),
            unresolved_count=sum(1 for m in all_mappings if m["status"] == "UNRESOLVED"),
            rejected_count=sum(1 for m in all_mappings if m["status"] == "REJECTED"),
        )

    run = mapping_store.start_or_resume_mapping_run(package["id"], schema_fingerprint, content_hash)

    load_result = load_vector_index(
        build_id, source_id, user_id, embedding_provider.provider_id,
        embedding_provider.model, embedding_provider.dimensions,
    )
    if load_result.status != STATUS_OK:
        mapping_store.fail_mapping_run(run["id"], {"reason": f"vector_index_status_{load_result.status}"})
        return MappingRunReport(
            package_id=package["id"], schema_fingerprint=schema_fingerprint, content_hash=content_hash,
            reused=False, provider_calls_made=0, batches_total=0, batches_completed=0,
            verified_count=0, unresolved_count=0, rejected_count=0,
            error=f"vector_index_status_{load_result.status}",
        )
    documents = load_result.index.documents
    documents_by_fqn_lower = {d.object_fqn.lower(): d for d in documents}

    authority_result = load_schema_object_authority(documents, build_id, source_id, schema_fingerprint)
    authority_by_fqn_lower = authority_result.authorities_by_fqn_lower

    concepts = package_service.list_concepts(package["id"])
    known_concept_ids = {c["concept_id"] for c in concepts}
    concepts_by_id = {c["concept_id"]: c for c in concepts}
    relationships = package_service.list_relationships(package["id"])
    program_family_by_concept_id = _compute_program_families(concepts, relationships)

    existing_hashes_by_concept = mapping_store.get_concept_content_hashes_by_schema_fingerprint(
        package["id"], schema_fingerprint,
    )
    forced = force_concept_ids or frozenset()
    pending_concepts = [
        c for c in concepts
        if c["concept_id"] in forced
        or _concept_content_hash(c) not in existing_hashes_by_concept.get(c["concept_id"], set())
    ]

    effective_batch_size = batch_size or BUSINESS_KNOWLEDGE_MAPPING_BATCH_SIZE
    batches = [
        pending_concepts[i:i + effective_batch_size]
        for i in range(0, len(pending_concepts), effective_batch_size)
    ]
    batches_total = run["batches_total"] or len(batches)
    batches_completed = 0
    provider_calls_made = 0
    total_storage_rejected = 0

    provider = mapping_provider or OpenAIBusinessKnowledgeMappingProvider()

    for batch in batches:
        candidate_docs_by_concept = {
            c["concept_id"]: _rank_candidate_objects(documents, c, limit=_MAX_CANDIDATE_OBJECTS_PER_BATCH)
            for c in batch
        }
        system_prompt, user_prompt = _build_prompt(batch, candidate_docs_by_concept)
        try:
            response = provider.propose_mappings(
                system_prompt, user_prompt, max_output_tokens=BUSINESS_KNOWLEDGE_MAPPING_MAX_OUTPUT_TOKENS,
            )
            provider_calls_made += 1
        except BusinessKnowledgeMappingProviderError as exc:
            mapping_store.fail_mapping_run(run["id"], {"reason": str(exc)})
            mapping_store.update_mapping_run_progress(
                run["id"], batches_total=batches_total, batches_completed=batches_completed,
                provider_calls_made=provider_calls_made,
            )
            return MappingRunReport(
                package_id=package["id"], schema_fingerprint=schema_fingerprint, content_hash=content_hash,
                reused=False, provider_calls_made=provider_calls_made, batches_total=batches_total,
                batches_completed=batches_completed, verified_count=0, unresolved_count=0, rejected_count=0,
                error=str(exc),
            )

        concept_hash_by_id = {c["concept_id"]: _concept_content_hash(c) for c in batch}
        raw_mappings = (response.raw or {}).get("mappings") or []
        storage_rejected_this_batch = 0
        for raw in raw_mappings:
            verified = _verify_proposal(
                raw, known_concept_ids=known_concept_ids, documents_by_fqn_lower=documents_by_fqn_lower,
                authority_by_fqn_lower=authority_by_fqn_lower,
                min_verified_confidence=BUSINESS_KNOWLEDGE_MAPPING_MIN_VERIFIED_CONFIDENCE,
                concepts_by_id=concepts_by_id, program_family_by_concept_id=program_family_by_concept_id,
                package_id=package["id"],
            )
            if verified is None:
                continue
            # A malformed/unknown concept_id has no batch entry to hash against
            # (verify_proposal already rejected it as "unknown_concept" in that
            # case) -- fall back to the whole-package hash only for that
            # never-resumable-anyway REJECTED row.
            per_concept_hash = concept_hash_by_id.get(verified.concept_id, content_hash)
            try:
                mapping_store.upsert_mapping(
                    package["id"], verified.concept_id, verified.object_fqn, verified.mapping_role,
                    columns=list(verified.columns), mapping_kind=verified.mapping_kind, status=verified.status,
                    confidence=verified.confidence, evidence=list(verified.evidence),
                    schema_fingerprint=schema_fingerprint, content_hash=per_concept_hash,
                    provider_id=provider.provider_id, model=provider.model,
                    contract_version=MAPPING_CONTRACT_VERSION, rejection_reason=verified.rejection_reason,
                )
            except sqlite3.IntegrityError as exc:
                # This database's own business_knowledge_mappings table does
                # not (yet) accept every value data.business_knowledge_
                # mapping_store.MAPPING_ROLES declares valid at the Python
                # level (e.g. an older, not-yet-migrated table on a
                # long-lived source). Never fatal: this one proposal is
                # dropped -- not silently, it is counted and logged -- and
                # every other proposal in this batch/run still persists
                # normally.
                storage_rejected_this_batch += 1
                logger.warning(
                    "run_business_knowledge_mapping: storage refused a verified proposal for "
                    "package_id=%s concept_id=%s mapping_role=%s (exception_type=%s); the row was "
                    "dropped, not persisted as VERIFIED/UNRESOLVED/REJECTED.",
                    package["id"], verified.concept_id, verified.mapping_role, type(exc).__name__,
                )

        batches_completed += 1
        mapping_store.update_mapping_run_progress(
            run["id"], batches_total=batches_total, batches_completed=batches_completed,
            provider_calls_made=provider_calls_made,
        )
        total_storage_rejected += storage_rejected_this_batch

    mapping_store.complete_mapping_run(run["id"])

    all_mappings = mapping_store.list_mappings(package["id"])
    rejected = [m for m in all_mappings if m["status"] == "REJECTED"]
    rejected_reasons: dict[str, int] = {}
    for m in rejected:
        reason = m["rejection_reason"] or "unknown"
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1

    return MappingRunReport(
        package_id=package["id"], schema_fingerprint=schema_fingerprint, content_hash=content_hash,
        reused=False, provider_calls_made=provider_calls_made, batches_total=batches_total,
        batches_completed=batches_completed,
        verified_count=sum(1 for m in all_mappings if m["status"] == "VERIFIED"),
        unresolved_count=sum(1 for m in all_mappings if m["status"] == "UNRESOLVED"),
        rejected_count=len(rejected), rejected_reasons=rejected_reasons,
        storage_rejected_count=total_storage_rejected,
    )
