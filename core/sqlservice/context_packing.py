"""
Deterministic, size-aware schema-context packing — SQL rebuild Phase 5,
Step 5.

Separates CANDIDATE DISCOVERY (done by core.sqlservice.schema_context_
retrieval's hybrid ranking and core.sqlservice.relationship_graph's bounded
path-finding) from FINAL PACKING (this module): given a priority-ordered
list of candidate documents, fit as many of the most useful ones as
possible into a hard character budget, never displacing an essential
high-priority candidate merely because an earlier, lower-priority candidate
happened to be large, and falling back to a deterministic compact
projection (built ONLY from already-computed structured metadata, never
invented) rather than dropping a candidate outright when its full document
text doesn't fit.

Scope: pure, in-memory, no I/O, no SQLite, no embedding/lexical scoring of
its own — every ranking/matching decision has already been made by the
caller before a candidate reaches this module. No specific customer
schema's table names or business vocabulary appear anywhere in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.sqlservice.lexical_index import tokenize

__all__ = [
    "SELECTION_REASON_HYBRID_ANCHOR",
    "SELECTION_REASON_RELATIONSHIP_BRIDGE",
    "SELECTION_REASON_DIRECT_RELATIONSHIP",
    "SELECTION_REASON_POOL_EXEMPT_NEIGHBOR",
    "SELECTION_REASON_EXAMPLE_DERIVED",
    "PackCandidate",
    "PackedItem",
    "PackResult",
    "build_compact_projection",
    "pack_candidates",
]

# ── Selection-reason vocabulary (Step 5 additions) ─────────────────────────
#
# core.sqlservice.schema_context_retrieval's pre-Step-5 SELECTION_REASON_
# SEMANTIC / SELECTION_REASON_RELATIONSHIP are UNCHANGED and still used
# verbatim by RANKING_MODE_VECTOR (the preserved pure-vector comparison
# mode, which never touches this module at all). The three reasons below
# are used only by RANKING_MODE_HYBRID's new candidate-pool/graph/packing
# pipeline.
SELECTION_REASON_HYBRID_ANCHOR = "HYBRID_ANCHOR"
SELECTION_REASON_RELATIONSHIP_BRIDGE = "RELATIONSHIP_BRIDGE"
SELECTION_REASON_DIRECT_RELATIONSHIP = "DIRECT_RELATIONSHIP"

# Step 7, Part 2 — a confirmed direct neighbor of a hybrid anchor, admitted
# even though it fell outside the fused candidate pool (unlike
# SELECTION_REASON_DIRECT_RELATIONSHIP, which is pool-scoped). See
# core.sqlservice.schema_context_retrieval's own pool-exempt selection pass
# for the eligibility/ranking/limit rules.
SELECTION_REASON_POOL_EXEMPT_NEIGHBOR = "POOL_EXEMPT_NEIGHBOR"

# Step 9 — an object a TRUSTED, compatible, similarity-threshold-passing
# retrieved SQL example proposed (core.sqlservice.sql_example_retrieval),
# admitted even when outside the fused candidate pool. Lowest packing
# priority of all five reasons — never displaces an anchor/bridge/direct/
# pool-exempt candidate, and still subject to the same tier and
# context-budget rules as everything else via pack_candidates().
SELECTION_REASON_EXAMPLE_DERIVED = "EXAMPLE_DERIVED"

_CONTEXT_SEPARATOR = "\n\n"


# ── Compact projection (structured metadata only, never invented) ─────────


def build_compact_projection(
    object_fqn: str,
    object_type: str,
    retrieval_tier: int,
    metadata: Mapping[str, object],
    query_tokens: Sequence[str],
    *,
    max_projected_columns: int,
) -> str | None:
    """
    A deterministic, compact text projection of one document, built ONLY
    from fields already present in `metadata` (see core.sqlservice.
    retrieval_documents' metadata contract) — never a heuristic summary,
    never an invented column/type/description, and never a truncation of
    document_text (this is a different, independently-generated text, not
    a substring of the full document).

    Returns None when there is nothing structurally meaningful to project
    (no business name, description, synonyms, primary key, foreign keys,
    confirmed relationships, or any column-derived facts at all) — the
    caller is expected to fall back to the full document text (or skip the
    candidate) in that case, per this step's own "retain the full document
    or skip it safely" requirement; this function itself never fabricates
    content to avoid returning None.

    `query_tokens` (already tokenized question text — see
    core.sqlservice.lexical_index.tokenize) is used only to select WHICH
    already-real synonyms/columns are worth surfacing (question-matched),
    never to invent a synonym or column that isn't already in metadata.
    """
    lines = [f"OBJECT: {object_fqn}", f"TYPE: {object_type}", f"TIER: {retrieval_tier}"]
    has_structured_facts = False
    query_token_set = set(query_tokens)

    business_name = metadata.get("business_name")
    if business_name:
        lines.append(f"BUSINESS_NAME: {business_name}")
        has_structured_facts = True

    description = metadata.get("description")
    if description:
        lines.append(f"DESCRIPTION: {description}")
        has_structured_facts = True

    synonyms = metadata.get("synonyms") or ()
    if query_token_set and synonyms:
        matched_synonyms = [s for s in synonyms if query_token_set & set(tokenize(s))]
        if matched_synonyms:
            lines.append(f"MATCHED_SYNONYMS: {', '.join(matched_synonyms)}")
            has_structured_facts = True

    primary_key_columns = metadata.get("primary_key_columns") or ()
    if primary_key_columns:
        lines.append(f"PRIMARY_KEY: {', '.join(primary_key_columns)}")
        has_structured_facts = True

    foreign_key_targets = metadata.get("foreign_key_targets") or ()
    if foreign_key_targets:
        lines.append(f"FOREIGN_KEYS: {', '.join(foreign_key_targets)}")
        has_structured_facts = True

    confirmed_targets = metadata.get("confirmed_relationship_targets") or ()
    if confirmed_targets:
        lines.append(f"RELATIONSHIPS: {', '.join(confirmed_targets)}")
        has_structured_facts = True

    confirmed_sources = metadata.get("confirmed_relationship_sources") or ()
    if confirmed_sources:
        lines.append(f"RELATIONSHIPS_INBOUND: {', '.join(confirmed_sources)}")
        has_structured_facts = True

    column_names = metadata.get("column_names") or ()
    if query_token_set and column_names:
        matched_columns = [c for c in column_names if query_token_set & set(tokenize(c))]
        if matched_columns:
            lines.append(f"MATCHED_COLUMNS: {', '.join(matched_columns[:max_projected_columns])}")
            has_structured_facts = True

    for label, key in (("STATUS_COLUMNS", "status_columns"), ("DATE_COLUMNS", "date_columns"), ("MEASURE_COLUMNS", "measure_columns")):
        values = metadata.get(key) or ()
        if values:
            lines.append(f"{label}: {', '.join(values[:max_projected_columns])}")
            has_structured_facts = True

    if not has_structured_facts:
        return None
    return "\n".join(lines)


# ── Priority-ordered, size-aware packing ───────────────────────────────────


@dataclass(frozen=True)
class PackCandidate:
    """One candidate offered to pack_candidates(), already in the priority
    order the caller wants respected (earlier entries are never displaced
    by later ones purely for size reasons). `doc` exposes object_fqn/
    object_type/retrieval_tier/document_hash/document_text/metadata (e.g.
    an IndexedSchemaDocument). `allow_compact` — whether a compact
    projection fallback is permitted for this candidate if its full text
    doesn't fit (True for anchors/bridges/direct relationships, which must
    not simply be dropped; False for lower-priority pool spillover, which
    is best-effort only and uses full text or is skipped, matching the
    pre-Step-5 simple-packing behavior for that tier)."""

    doc: object
    reason: str
    components: Mapping[str, object]  # score/semantic_rank/lexical_rank/lexical_score/fused_score/matched_lexical_fields
    allow_compact: bool


@dataclass(frozen=True)
class PackedItem:
    """One item pack_candidates() actually included."""

    doc: object
    reason: str
    components: Mapping[str, object]
    context_text: str          # the text actually placed into context_text — full or compact
    used_compact_text: bool


@dataclass(frozen=True)
class PackResult:
    packed: tuple[PackedItem, ...]
    warnings: tuple[str, ...]
    truncated: bool
    used_characters: int


def pack_candidates(
    candidates: Sequence[PackCandidate],
    *,
    max_context_characters: int,
    query_tokens: Sequence[str],
    max_projected_columns: int,
) -> PackResult:
    """
    Fit `candidates` (already deduplicated and in priority order) into
    `max_context_characters`, never splitting a document mid-line/mid-
    object.

    For each candidate, in order:
      1. Try its full document_text. If it fits in the REMAINING budget,
         use it — this is what guarantees an earlier (higher-priority)
         candidate is never skipped just because trying it first would
         have left less room for something later; packing always considers
         candidates strictly in priority order, never reorders by size.
      2. If it doesn't fit and allow_compact is True, build a compact
         projection (see build_compact_projection) and try that instead —
         this is what lets a large ESSENTIAL candidate (an anchor or a
         required join bridge) survive the budget instead of being
         dropped, satisfying "essential anchors and required bridges must
         not be displaced merely because unrelated earlier-ranked
         documents are large": since anchors/bridges are placed EARLY in
         `candidates` by the caller, they get first claim on the budget in
         full form, and compaction is only a fallback for the (rarer) case
         where even an early candidate's full text is too large.
      3. If neither fits (or allow_compact is False and the full text
         doesn't fit, or a compact projection isn't possible because no
         structured facts exist to project), the candidate is skipped with
         a clear warning — the hard budget is never exceeded to force a
         fit (requirement: exact character-budget enforcement).

    Deterministic and never mid-fragment: every included text is either a
    document's complete document_text or a complete compact projection,
    joined by the same "\\n\\n" separator used throughout this project.
    """
    packed: list[PackedItem] = []
    warnings: list[str] = []
    used_characters = 0
    truncated = False
    has_prior = False

    for candidate in candidates:
        doc = candidate.doc
        sep_cost = len(_CONTEXT_SEPARATOR) if has_prior else 0

        full_cost = len(doc.document_text) + sep_cost
        if used_characters + full_cost <= max_context_characters:
            packed.append(PackedItem(
                doc=doc, reason=candidate.reason, components=candidate.components,
                context_text=doc.document_text, used_compact_text=False,
            ))
            used_characters += full_cost
            has_prior = True
            continue

        if candidate.allow_compact:
            compact_text = build_compact_projection(
                doc.object_fqn, doc.object_type, doc.retrieval_tier, doc.metadata,
                query_tokens, max_projected_columns=max_projected_columns,
            )
            if compact_text is not None:
                compact_cost = len(compact_text) + sep_cost
                if used_characters + compact_cost <= max_context_characters:
                    packed.append(PackedItem(
                        doc=doc, reason=candidate.reason, components=candidate.components,
                        context_text=compact_text, used_compact_text=True,
                    ))
                    used_characters += compact_cost
                    has_prior = True
                    truncated = True
                    warnings.append(
                        f"compacted {doc.object_fqn!r} ({candidate.reason}) for budget: "
                        f"full document ({len(doc.document_text)} chars) did not fit, "
                        f"used a {len(compact_text)}-character structured projection instead"
                    )
                    continue

        truncated = True
        warnings.append(
            f"skipped {doc.object_fqn!r} ({candidate.reason}): would exceed the "
            f"{max_context_characters}-character budget"
            + ("" if candidate.allow_compact else " (no compact fallback for this priority tier)")
        )

    return PackResult(
        packed=tuple(packed), warnings=tuple(warnings), truncated=truncated, used_characters=used_characters,
    )
