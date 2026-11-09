"""
Business-knowledge evidence for question-time schema selection -- Phase 7,
Step 3J, Part 5.

Reads a source's ACTIVE business-knowledge package's VERIFIED/UNRESOLVED
schema mappings (core.sqlservice.business_knowledge_mapper /
data.business_knowledge_mapping_store) and exposes them per real object_fqn,
for core.sqlservice.schema_frontier to attach onto its own FrontierCandidate
records as ADDITIONAL evidence -- never a replacement for vector/lexical/
business-concept retrieval, never an unconditional table override (a mapping
here is one more structured fact the GPT-6 Sol selector reasons over
alongside real schema/authority evidence, exactly like core.sqlservice.
schema_object_authority's own authority_* fields).

Trust boundary: only VERIFIED and UNRESOLVED mappings ever reach this
result -- a REJECTED mapping (hallucinated object/column, ineligible tier,
unsafe dependency; see business_knowledge_mapper's own docstring) never
surfaces here at all, at any confidence. UNRESOLVED mappings DO surface
(callers can see them and their real, sub-threshold confidence), but their
mapping_status field always says so -- never disguised as VERIFIED.

Zero behavior change for a source with no ACTIVE business-knowledge
package (or when core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED is off):
load_business_knowledge_evidence returns None, and every existing caller
treats that exactly like "no evidence at all" -- the same generic vector/
lexical/business-concept/authority retrieval path that already exists for
every source runs completely unchanged.

Cache identity is (package_id, schema_fingerprint) -- a mapping run only
ever writes rows tagged with the schema_fingerprint it was computed against
(business_knowledge_mapper), so a NEW schema_fingerprint (real schema
change) naturally misses this cache instead of serving stale evidence; an
ordinary reconnect to the identical schema is a cache hit.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import Mapping

import data.business_knowledge_mapping_store as mapping_store
import data.business_knowledge_package_service as package_service

__all__ = [
    "BusinessKnowledgeMappingEvidence",
    "BusinessKnowledgeEvidenceIdentity",
    "BusinessKnowledgeEvidenceResult",
    "load_business_knowledge_evidence",
    "clear_business_knowledge_evidence_cache",
    "business_knowledge_evidence_cache_size",
]

_MAX_CACHE_ENTRIES = 16
_SURFACED_STATUSES = frozenset({"VERIFIED", "UNRESOLVED"})
# Phase 7, Step 3J1 fix -- an AI mapping-proposal's own "evidence" strings are
# UNVALIDATED free text (core.sqlservice.business_knowledge_mapper never
# bounds their length, since they might legitimately be a short human-
# readable justification). In practice a provider sometimes echoes back a
# large fraction of the candidate description it was given (e.g. a 20+
# column list) verbatim as its own "evidence" -- observed live against a
# wide real table, where this pushed core.sqlservice.schema_frontier's
# per-object character budget (DEFAULT_MAX_CHARACTERS_PER_OBJECT=600) over
# the edge and caused business_knowledge_mappings to be silently dropped by
# that budget's own last-resort optional-section trim, for exactly the
# object a business question most needed it on. Bounding here (the single
# choke point every caller reads through, regardless of when/how a mapping
# was generated) protects already-stored rows too, without needing to
# rewrite them.
_MAX_EVIDENCE_STRINGS_PER_MAPPING = 3
_MAX_EVIDENCE_STRING_LENGTH = 120


def _bounded_evidence(raw_evidence: list) -> tuple[str, ...]:
    bounded: list[str] = []
    for item in raw_evidence[:_MAX_EVIDENCE_STRINGS_PER_MAPPING]:
        text = str(item)
        if len(text) > _MAX_EVIDENCE_STRING_LENGTH:
            text = text[:_MAX_EVIDENCE_STRING_LENGTH].rstrip() + "..."
        bounded.append(text)
    return tuple(bounded)

_cache: "OrderedDict[BusinessKnowledgeEvidenceIdentity, BusinessKnowledgeEvidenceResult]" = OrderedDict()
_cache_lock = RLock()


@dataclass(frozen=True)
class BusinessKnowledgeMappingEvidence:
    """One bounded, immutable justification for why an object matters to a
    business concept. `concept_origin` is the underlying CONCEPT's own
    origin (AUTHORITATIVE = owner-confirmed, AI_INFERRED = AI-proposed) --
    independent of `mapping_status`, which is this MAPPING's own
    deterministic-verification outcome (VERIFIED or UNRESOLVED; a REJECTED
    mapping never reaches this dataclass at all -- see this module's own
    docstring)."""

    concept_id: str
    concept_type: str
    concept_origin: str
    mapping_role: str
    mapping_status: str
    confidence: float
    evidence: tuple[str, ...]
    # Phase 7, Step 6E1 — the mapping's own approved column list (data.
    # business_knowledge_mapping_store's `columns` field), carried through
    # so core.sqlservice.schema_frontier's Tier 3/4 governance force-admit
    # can restrict what it renders to exactly these columns. Empty tuple
    # (the default) means the mapping named no specific columns -- callers
    # must never treat that as "every column approved".
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class BusinessKnowledgeEvidenceIdentity:
    package_id: int
    schema_fingerprint: str
    # Phase 7, Step 6E1, Part 4 fix -- a mapping row can change (a human
    # adds/edits/revokes a VERIFIED governance mapping, e.g. via
    # data.business_knowledge_mapping_store.upsert_mapping) without either
    # package_id or schema_fingerprint changing, which previously let this
    # cache silently keep serving pre-edit evidence for up to _MAX_CACHE_
    # ENTRIES worth of (package_id, schema_fingerprint) combinations for the
    # lifetime of the process. Cheap (count, latest updated_at) signature
    # over this package's own mapping rows -- same idiom as core.sqlservice.
    # schema_object_authority._authority_state -- so any mapping change
    # naturally misses this cache instead of ever serving stale governance
    # evidence.
    mapping_state: tuple = ()


@dataclass(frozen=True)
class BusinessKnowledgeEvidenceResult:
    identity: BusinessKnowledgeEvidenceIdentity
    mappings_by_object_fqn_lower: Mapping[str, tuple[BusinessKnowledgeMappingEvidence, ...]]


def _mapping_state(package_id: int) -> tuple:
    """Cheap (count, latest updated_at) signature over this package's own
    business_knowledge_mappings rows -- comparison-only, never treated as
    semantic evidence itself. Fails open (empty state) if the table is
    missing/not yet migrated, so a partially-migrated database still
    produces a stable, cacheable identity rather than raising."""
    from data.db import get_connection

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS m "
            "FROM business_knowledge_mappings WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        return (int(row["c"] or 0), str(row["m"] or ""))
    except Exception:  # noqa: BLE001 -- a missing/not-yet-migrated table must never break retrieval
        return (0, "")
    finally:
        conn.close()


def load_business_knowledge_evidence(
    source_id: int, user_id: str, schema_fingerprint: str | None, *, use_cache: bool = True,
) -> BusinessKnowledgeEvidenceResult | None:
    """Returns None when source_id has no ACTIVE business-knowledge package,
    or schema_fingerprint is falsy -- callers must treat None exactly like
    "no evidence" (see this module's own docstring on the fallback
    guarantee). Never calls an AI provider, never makes a new schema probe;
    reads only already-persisted, already-verified mapping rows."""
    if not schema_fingerprint:
        return None
    package = package_service.get_active_package(source_id, user_id)
    if package is None:
        return None

    identity = BusinessKnowledgeEvidenceIdentity(
        package_id=package["id"], schema_fingerprint=schema_fingerprint,
        mapping_state=_mapping_state(package["id"]),
    )
    with _cache_lock:
        if use_cache and identity in _cache:
            _cache.move_to_end(identity)
            return _cache[identity]

    mappings = mapping_store.list_mappings(package["id"], schema_fingerprint=schema_fingerprint)
    concepts_by_id = {c["concept_id"]: c for c in package_service.list_concepts(package["id"])}

    by_object: dict[str, list[BusinessKnowledgeMappingEvidence]] = {}
    for m in mappings:
        if m["status"] not in _SURFACED_STATUSES:
            continue
        concept = concepts_by_id.get(m["concept_id"])
        if concept is None:
            continue
        by_object.setdefault(m["object_fqn"].lower(), []).append(
            BusinessKnowledgeMappingEvidence(
                concept_id=m["concept_id"], concept_type=concept["concept_type"],
                concept_origin=concept["origin"], mapping_role=m["mapping_role"],
                mapping_status=m["status"], confidence=m["confidence"],
                evidence=_bounded_evidence(m["evidence"]),
                columns=tuple(str(c).strip() for c in (m.get("columns") or ()) if str(c).strip()),
            )
        )

    result = BusinessKnowledgeEvidenceResult(
        identity=identity,
        mappings_by_object_fqn_lower={k: tuple(v) for k, v in by_object.items()},
    )
    with _cache_lock:
        if use_cache:
            _cache[identity] = result
            _cache.move_to_end(identity)
            while len(_cache) > _MAX_CACHE_ENTRIES:
                _cache.popitem(last=False)
    return result


def clear_business_knowledge_evidence_cache() -> None:
    with _cache_lock:
        _cache.clear()


def business_knowledge_evidence_cache_size() -> int:
    with _cache_lock:
        return len(_cache)
