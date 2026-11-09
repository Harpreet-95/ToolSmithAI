"""
Exact schema-context construction for an AI-selected, relationship-completed
object set — SQL rebuild Phase 7, Step 3C, Part 5.

Built ONLY after core.sqlservice.schema_selection_service has chosen the
business objects and core.sqlservice.schema_relationship_completion has
verified/bridged their confirmed joins. Includes full bounded schema detail
(canonical columns, PK/FK facts, confirmed executable join column pairs,
trusted business metadata) for exactly those objects — never the remaining
frontier.

Deliberately returns a core.sqlservice.schema_context_retrieval.
SchemaContextResult (built from ContextDocument entries) rather than a new,
parallel result type: core.sqlservice.sql_generation_service.
generate_sql_candidate, the retired SQL-review stage.
review_sql_candidate, and core.sqlservice.sql_validation.
validate_sql_candidate already accept exactly that shape and already treat
any non-STATUS_OK `status` as "invalid context, short-circuit" — reusing it
means this module needs zero adapter code and those three downstream stages
are used completely unchanged (Part 6, requirement: reuse SQL generation/
review/validation exactly as they already exist).

Reuses core.sqlservice.context_packing.pack_candidates verbatim (the SAME
priority-ordered, size-aware, compact-fallback packer core.sqlservice.
schema_context_retrieval already relies on) rather than reimplementing
budget enforcement. Unlike that module's own anchor/bridge/direct/spillover
priority tiers (where a low-priority candidate may be silently dropped),
EVERY object this module is asked to pack is essential by construction
(either AI-selected or a required confirmed connector) — so a candidate
that cannot be packed even compactly means the whole context is incomplete,
reported distinctly (a non-OK status) rather than silently proceeding with
a partial object set, per Part 5's own "return clarification rather than
silently dropping an essential object" requirement.
"""

from __future__ import annotations

from typing import Sequence

from core.config import AI_FIRST_SQL_MAX_CONTEXT_CHARACTERS
from core.sqlservice.context_packing import PackCandidate, pack_candidates
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.retrieval_store import get_retrieval_build
from core.sqlservice.schema_context_retrieval import STATUS_OK as _CONTEXT_STATUS_OK
from core.sqlservice.schema_context_retrieval import (
    DEFAULT_ALLOWED_TIERS,
    DEFAULT_MAX_PROJECTED_COLUMNS,
    DEFAULT_RANKING_MODE,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.vector_index import STATUS_OK as _INDEX_STATUS_OK
from core.sqlservice.vector_index import load_vector_index

__all__ = [
    "STATUS_OK",
    "STATUS_INCOMPLETE",
    "SELECTION_ROLE_AI_SELECTED",
    "SELECTION_ROLE_CONNECTOR",
    "build_exact_schema_context",
]

STATUS_OK = _CONTEXT_STATUS_OK
STATUS_INCOMPLETE = "INCOMPLETE"  # an essential object/connector could not be packed, even compactly

SELECTION_ROLE_AI_SELECTED = "AI_SELECTED"
SELECTION_ROLE_CONNECTOR = "CONFIRMED_CONNECTOR"

_ALL_TIERS = frozenset({1, 2, 3, 4})


def _empty_result(
    status: str, build_id: int, provider: EmbeddingProvider, error: str | None, warnings: Sequence[str] = (),
) -> SchemaContextResult:
    return SchemaContextResult(
        status=status, build_id=build_id, schema_fingerprint=None,
        embedding_provider=provider.provider_id, embedding_model=provider.model,
        embedding_dimensions=provider.dimensions, documents=(), context_text="", total_characters=0,
        truncated=False, warnings=tuple(warnings), error=error, ranking_mode=DEFAULT_RANKING_MODE,
        essential_context_incomplete=bool(warnings),
    )


def build_exact_schema_context(
    selected_object_fqns: Sequence[str],
    connector_object_fqns: Sequence[str],
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    allowed_tiers: frozenset[int] | None = None,
    max_context_characters: int = AI_FIRST_SQL_MAX_CONTEXT_CHARACTERS,
    max_projected_columns_per_object: int = DEFAULT_MAX_PROJECTED_COLUMNS,
) -> SchemaContextResult:
    """
    Build the exact, final SQL-generation schema context for exactly
    `selected_object_fqns` (packed first — the AI's own business-relevant
    choices) followed by `connector_object_fqns` (packed second — required
    for joinability, never the reason SQL is generated in the first place).
    Each resulting ContextDocument.selection_reason is SELECTION_ROLE_
    AI_SELECTED or SELECTION_ROLE_CONNECTOR — distinct string values from
    every SELECTION_REASON_* this pipeline's legacy top-25 packing uses, so
    a caller can always tell which stage produced a given context.

    `provider` supplies ONLY the (provider_id, model, dimensions) identity
    needed to reuse the already-cached VectorIndex for this build — no
    embedding call, no AI call.

    Order of operations:
      1. Validate parameters — raises ValueError, no I/O yet.
      2. load_vector_index() — a cache hit reusing the SAME index core.
         sqlservice.schema_frontier / core.sqlservice.
         schema_relationship_completion already loaded for this build. A
         non-OK status propagates unchanged (as this result's own
         `status`) — generate_sql_candidate already treats any non-OK
         status as STATUS_INVALID_CONTEXT, so no adapter is needed here.
      3. Any selected/connector object absent from the tier-allowed
         document set: STATUS_INCOMPLETE.
      4. pack_candidates() — full document text first, a compact
         structured projection as a fallback, hard `max_context_characters`
         budget, exactly as core.sqlservice.schema_context_retrieval's own
         anchor/bridge packing already works. Every candidate here is
         essential by construction: if ANY of them could not be packed
         even compactly, the whole result is STATUS_INCOMPLETE rather than
         a silently partial context.
    """
    if not isinstance(selected_object_fqns, Sequence) or isinstance(selected_object_fqns, (str, bytes)) or not selected_object_fqns:
        raise ValueError("selected_object_fqns must be a non-empty sequence of strings")
    if not isinstance(connector_object_fqns, Sequence) or isinstance(connector_object_fqns, (str, bytes)):
        raise ValueError("connector_object_fqns must be a sequence of strings")
    selected = [str(fqn).strip() for fqn in selected_object_fqns]
    connectors = [str(fqn).strip() for fqn in connector_object_fqns]
    if any(not fqn for fqn in (*selected, *connectors)):
        raise ValueError("selected_object_fqns/connector_object_fqns must not contain blank entries")
    selected_lower = [fqn.lower() for fqn in selected]
    connector_lower = [fqn.lower() for fqn in connectors]
    if len(set(selected_lower)) != len(selected_lower):
        raise ValueError("selected_object_fqns must not contain duplicate (case-insensitive) entries")
    if len(set(connector_lower)) != len(connector_lower):
        raise ValueError("connector_object_fqns must not contain duplicate (case-insensitive) entries")
    if set(selected_lower) & set(connector_lower):
        raise ValueError("connector_object_fqns must not overlap with selected_object_fqns")
    if not isinstance(max_context_characters, int) or isinstance(max_context_characters, bool) or max_context_characters <= 0:
        raise ValueError(f"max_context_characters must be a positive integer, got {max_context_characters!r}")
    if (
        not isinstance(max_projected_columns_per_object, int)
        or isinstance(max_projected_columns_per_object, bool)
        or max_projected_columns_per_object <= 0
    ):
        raise ValueError(
            f"max_projected_columns_per_object must be a positive integer, got {max_projected_columns_per_object!r}"
        )

    if allowed_tiers is None:
        effective_tiers = DEFAULT_ALLOWED_TIERS
    else:
        effective_tiers = frozenset(allowed_tiers)
        if not effective_tiers or not effective_tiers.issubset(_ALL_TIERS):
            raise ValueError(f"allowed_tiers must be a non-empty subset of {sorted(_ALL_TIERS)}, got {allowed_tiers!r}")

    load_result = load_vector_index(build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions)
    if load_result.status != _INDEX_STATUS_OK:
        return _empty_result(load_result.status, build_id, provider, load_result.error)

    tier_allowed = {d.object_fqn.lower(): d for d in load_result.index.documents if d.retrieval_tier in effective_tiers}

    # Phase 7, Step 6E, Part 4 fix — every object in `selected_object_fqns`/
    # `connector_object_fqns` is essential BY CONSTRUCTION (the caller has
    # already committed to it, and core.sqlservice.schema_relationship_
    # completion.complete_relationships already verified confirmed-
    # relationship connectivity across exactly this same set, itself
    # applying the identical caller-specified-object exception — Phase 7,
    # Step 6E, Part 4). Excluding one here purely because of its own
    # retrieval tier — e.g. a real business attribute that happens to live
    # in a generic Tier 3 framework table — would make a correctly
    # connectivity-verified selection unconditionally unpackable. This
    # never widens tier eligibility for anything the caller did not
    # explicitly name.
    essential_lower = {fqn.lower() for fqn in (*selected, *connectors)}
    for doc in load_result.index.documents:
        if doc.object_fqn.lower() in essential_lower:
            tier_allowed.setdefault(doc.object_fqn.lower(), doc)

    missing = sorted({fqn for fqn in (*selected, *connectors) if fqn.lower() not in tier_allowed})
    if missing:
        return _empty_result(
            STATUS_INCOMPLETE, build_id, provider,
            error=f"essential object(s) not present in the allowed schema index: {missing}",
            warnings=(f"missing essential object(s): {missing}",),
        )

    pack_input: list[PackCandidate] = [
        PackCandidate(doc=tier_allowed[fqn.lower()], reason=SELECTION_ROLE_AI_SELECTED, components={}, allow_compact=True)
        for fqn in selected
    ] + [
        PackCandidate(doc=tier_allowed[fqn.lower()], reason=SELECTION_ROLE_CONNECTOR, components={}, allow_compact=True)
        for fqn in connectors
    ]

    pack_result = pack_candidates(
        pack_input, max_context_characters=max_context_characters, query_tokens=(),
        max_projected_columns=max_projected_columns_per_object,
    )

    packed_lower = {item.doc.object_fqn.lower() for item in pack_result.packed}
    all_requested_lower = set(selected_lower) | set(connector_lower)
    unpacked = sorted(fqn for fqn in (*selected, *connectors) if fqn.lower() not in packed_lower)
    if unpacked or packed_lower != all_requested_lower:
        return _empty_result(
            STATUS_INCOMPLETE, build_id, provider,
            error=(
                f"essential object(s) could not be packed within the {max_context_characters}-character "
                f"budget, even compactly: {unpacked}"
            ),
            warnings=tuple(pack_result.warnings) or (f"essential object(s) not packed: {unpacked}",),
        )

    documents = tuple(
        ContextDocument(
            object_fqn=item.doc.object_fqn, object_type=item.doc.object_type, retrieval_tier=item.doc.retrieval_tier,
            document_hash=item.doc.document_hash, document_text=item.doc.document_text, metadata=item.doc.metadata,
            score=None, selection_reason=item.reason, used_compact_text=item.used_compact_text,
        )
        for item in pack_result.packed
    )
    context_text = "\n\n".join(item.context_text for item in pack_result.packed)

    build_row = get_retrieval_build(build_id, source_id, user_id)
    schema_fingerprint = build_row.schema_fingerprint if build_row is not None else None

    return SchemaContextResult(
        status=STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider=provider.provider_id, embedding_model=provider.model,
        embedding_dimensions=provider.dimensions, documents=documents, context_text=context_text,
        total_characters=len(context_text), truncated=bool(pack_result.warnings), warnings=tuple(pack_result.warnings),
        error=None, ranking_mode=DEFAULT_RANKING_MODE, essential_context_incomplete=False,
    )
