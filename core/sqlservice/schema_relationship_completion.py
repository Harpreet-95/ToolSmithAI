"""
Confirmed relationship completion for an AI-selected object set — SQL
rebuild Phase 7, Step 3C, Part 4.

Factual schema enforcement, NOT deterministic business-intent selection:
given the objects core.sqlservice.schema_selection_service already chose on
business-intent grounds, this module's only job is to verify (and, where
necessary, bridge) that they are actually joinable using real, confirmed
relationships — reusing core.sqlservice.relationship_graph exactly as
core.sqlservice.schema_context_retrieval's own hybrid-anchor bridging
already does (same build_relationship_graph/find_shortest_relationship_path
calls, same tier-allowed/confirmed-only graph construction), never a new
graph algorithm or a new notion of "confirmed".

Weak, inferred, pending, rejected, cross-source, Tier 3/4, archive, and
system edges are excluded by construction — core.sqlservice.
relationship_graph has no notion of any edge kind other than "confirmed" at
all (see its own docstring), and this module only ever builds that graph
from tier-allowed documents (excludes Tier 3/4/archive/system before the
graph even exists) belonging to the SAME build (one build is always scoped
to one source, so a cross-source edge cannot structurally appear).

Never guesses a join: when the selected objects cannot be safely connected
within the configured bounds, this module reports STATUS_DISCONNECTED and
adds zero connectors — the caller (core.sqlservice.
the AI-first fast path) is expected
to short-circuit to a clarification rather than proceed to SQL generation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from core.config import AI_FIRST_SCHEMA_MAX_CONNECTORS
from core.sqlservice.embedding_providers import EmbeddingProvider
from core.sqlservice.relationship_graph import (
    MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH,
    build_relationship_graph,
    find_shortest_relationship_path,
)
from core.sqlservice.schema_context_retrieval import DEFAULT_ALLOWED_TIERS, DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH
from core.sqlservice.vector_index import STATUS_OK as _INDEX_STATUS_OK
from core.sqlservice.vector_index import load_vector_index

__all__ = [
    "STATUS_CONNECTED",
    "STATUS_DISCONNECTED",
    "MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH",
    "RelationshipCompletionResult",
    "complete_relationships",
]

STATUS_CONNECTED = "CONNECTED"
STATUS_DISCONNECTED = "DISCONNECTED"  # selected objects could not be safely, confirmedly connected

_ALL_TIERS = frozenset({1, 2, 3, 4})


@dataclass(frozen=True)
class RelationshipCompletionResult:
    """
    Result of one complete_relationships() call.

    status: STATUS_CONNECTED | STATUS_DISCONNECTED | one of core.sqlservice.
        vector_index's own STATUS_* values (propagated unchanged when the
        build's index itself could not be loaded — zero graph work done in
        that case).
    connector_object_fqns: the bounded, deterministic (sorted) set of
        REQUIRED confirmed-relationship objects added to join the selected
        set — always disjoint from `selected_object_fqns`, always empty
        when status != STATUS_CONNECTED. Distinct from `selected_object_
        fqns` (requirement 7 — "preserve selected AI objects distinctly
        from automatically added connectors for observability").
    """

    status: str
    selected_object_fqns: tuple[str, ...]
    connector_object_fqns: tuple[str, ...]
    warnings: tuple[str, ...]
    error: str | None


def _result(
    status: str, selected_object_fqns: Sequence[str], connector_object_fqns: Sequence[str] = (),
    warnings: Sequence[str] = (), error: str | None = None,
) -> RelationshipCompletionResult:
    return RelationshipCompletionResult(
        status=status, selected_object_fqns=tuple(selected_object_fqns),
        connector_object_fqns=tuple(connector_object_fqns), warnings=tuple(warnings), error=error,
    )


def complete_relationships(
    selected_object_fqns: Sequence[str],
    build_id: int,
    source_id: int,
    user_id: str,
    provider: EmbeddingProvider,
    *,
    allowed_tiers: frozenset[int] | None = None,
    max_relationship_path_length: int = DEFAULT_MAX_RELATIONSHIP_PATH_LENGTH,
    max_connectors: int = AI_FIRST_SCHEMA_MAX_CONNECTORS,
) -> RelationshipCompletionResult:
    """
    Verify/bridge confirmed-relationship connectivity across
    `selected_object_fqns` (the AI schema selector's own output).

    `provider` supplies ONLY the (provider_id, model, dimensions) identity
    needed to locate the already-built, already-cached VectorIndex for this
    build (core.sqlservice.vector_index.load_vector_index) — this function
    never embeds anything and never makes an AI call.

    Order of operations:
      1. Validate parameters — raises ValueError, no I/O yet.
      2. load_vector_index() — reuses the SAME cached index
         core.sqlservice.schema_frontier already loaded for this build (a
         cache hit, not a rebuild) for its own ownership/READY/completeness
         gate. A non-OK status propagates unchanged, zero graph work.
      3. 0 or 1 selected object: trivially connected, zero connectors
         needed.
      4. Any selected object absent from the tier-allowed document set (not
         indexed at all, or filtered out by `allowed_tiers`) makes the
         whole set unsafe to connect: STATUS_DISCONNECTED, zero SQL
         generation risk.
      5. Build the confirmed-relationship graph over tier-allowed documents
         only (core.sqlservice.relationship_graph.build_relationship_graph
         — confirmed edges only, by that module's own construction).
      6. For each deterministic (sorted by object_fqn) consecutive pair of
         selected objects, find the shortest confirmed path (bounded by
         `max_relationship_path_length`, hard-capped at
         MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH=3) — mirrors core.sqlservice.
         schema_context_retrieval's own hybrid-anchor bridging exactly.
         Any missing pairwise path makes the whole set STATUS_DISCONNECTED
         — never a partial/best-effort join.
      7. The union of every path's intermediate nodes (excluding the
         selected objects themselves) becomes `connector_object_fqns`,
         sorted, deduplicated. Exceeding `max_connectors` is treated the
         same as an unreachable path — STATUS_DISCONNECTED, never a
         silently truncated connector set (a truncated connector set could
         produce an invalid/incomplete join).

    Never invents a join: only edges the SAME confirmed-relationship graph
    already used elsewhere in this pipeline are ever considered.
    """
    if not isinstance(selected_object_fqns, Sequence) or isinstance(selected_object_fqns, (str, bytes)):
        raise ValueError("selected_object_fqns must be a sequence of strings")
    cleaned = [str(fqn).strip() for fqn in selected_object_fqns]
    if not cleaned or any(not fqn for fqn in cleaned):
        raise ValueError("selected_object_fqns must be a non-empty sequence of non-blank strings")
    if len({fqn.lower() for fqn in cleaned}) != len(cleaned):
        raise ValueError("selected_object_fqns must not contain duplicate (case-insensitive) entries")
    if (
        not isinstance(max_relationship_path_length, int)
        or isinstance(max_relationship_path_length, bool)
        or max_relationship_path_length <= 0
    ):
        raise ValueError(
            f"max_relationship_path_length must be a positive integer, got {max_relationship_path_length!r}"
        )
    if max_relationship_path_length > MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH:
        raise ValueError(
            f"max_relationship_path_length={max_relationship_path_length} exceeds the hard cap of "
            f"{MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH} edges"
        )
    if not isinstance(max_connectors, int) or isinstance(max_connectors, bool) or max_connectors < 0:
        raise ValueError(f"max_connectors must be a non-negative integer, got {max_connectors!r}")

    if allowed_tiers is None:
        effective_tiers = DEFAULT_ALLOWED_TIERS
    else:
        effective_tiers = frozenset(allowed_tiers)
        if not effective_tiers or not effective_tiers.issubset(_ALL_TIERS):
            raise ValueError(f"allowed_tiers must be a non-empty subset of {sorted(_ALL_TIERS)}, got {allowed_tiers!r}")

    load_result = load_vector_index(build_id, source_id, user_id, provider.provider_id, provider.model, provider.dimensions)
    if load_result.status != _INDEX_STATUS_OK:
        return _result(load_result.status, cleaned, error=load_result.error)

    if len(cleaned) <= 1:
        return _result(STATUS_CONNECTED, cleaned)

    tier_allowed_docs = [d for d in load_result.index.documents if d.retrieval_tier in effective_tiers]

    # Phase 7, Step 6E, Part 4 fix — an object the caller explicitly
    # SELECTED must always be an eligible ENDPOINT of its own connectivity
    # check, regardless of its own retrieval tier. core.sqlservice.
    # schema_frontier's own confirmed-relationship force-admit (Phase 7,
    # Step 6E, Part 4) already lets a Tier 3/4 object reach the selector
    # when a real, structurally-confirmed relationship justifies it (e.g. a
    # generic framework "Users" table an entity links to by a declared FK)
    # — excluding that SAME object from the graph verifying it is safely
    # joinable, purely because of its tier, would make a correct selection
    # unconditionally unconfirmable. Only a Tier 3/4 CONNECTOR (an
    # intermediate, unselected bridge node on a path between two OTHER
    # selected objects) remains excluded, unchanged — this never widens
    # what can be used to BRIDGE two objects, only what a selected object
    # itself may be.
    selected_lower_set = {fqn.lower() for fqn in cleaned}
    tier_allowed_by_lower = {d.object_fqn.lower(): d for d in tier_allowed_docs}
    for doc in load_result.index.documents:
        if doc.object_fqn.lower() in selected_lower_set:
            tier_allowed_by_lower.setdefault(doc.object_fqn.lower(), doc)
    tier_allowed_docs = list(tier_allowed_by_lower.values())
    fqn_by_lower = {d.object_fqn.lower(): d.object_fqn for d in tier_allowed_docs}

    selected_lower = sorted({fqn.lower() for fqn in cleaned})
    missing = [fqn for fqn in cleaned if fqn.lower() not in fqn_by_lower]
    if missing:
        return _result(
            STATUS_DISCONNECTED, cleaned,
            warnings=(
                f"selected object(s) not found among tier-allowed documents for this build: {sorted(missing)}",
            ),
            error="one or more selected objects are not connectable — not present in the allowed schema index",
        )

    graph = build_relationship_graph(tier_allowed_docs)

    connectors: set[str] = set()
    for left, right in zip(selected_lower, selected_lower[1:]):
        path = find_shortest_relationship_path(graph, left, right, max_path_length=max_relationship_path_length)
        if path is None:
            return _result(
                STATUS_DISCONNECTED, cleaned,
                warnings=(
                    f"no confirmed relationship path of at most {max_relationship_path_length} edges connects "
                    f"{fqn_by_lower[left]!r} and {fqn_by_lower[right]!r}",
                ),
                error="selected objects cannot be safely connected using only confirmed relationships",
            )
        for node in path[1:-1]:
            if node not in set(selected_lower):
                connectors.add(node)

    if len(connectors) > max_connectors:
        return _result(
            STATUS_DISCONNECTED, cleaned,
            warnings=(
                f"required confirmed connector count ({len(connectors)}) exceeds max_connectors={max_connectors}",
            ),
            error="selected objects require more confirmed connectors than the configured bound allows",
        )

    connector_fqns = sorted(fqn_by_lower[key] for key in connectors)
    return _result(STATUS_CONNECTED, cleaned, connector_fqns)
