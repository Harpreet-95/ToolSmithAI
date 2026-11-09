"""
In-memory confirmed-relationship graph and bounded shortest-path lookup —
SQL rebuild Phase 5, Step 5.

Built entirely from the SAME structured metadata keys
core.sqlservice.schema_context_retrieval's own one-hop expansion already
reads — "confirmed_relationship_targets" (outbound) and
"confirmed_relationship_sources" (inbound), as written by
core.sqlservice.retrieval_documents.build_retrieval_documents(). Never
"inferred_relationship_*" — this module has no notion of an inferred edge
at all, by construction, not merely by a runtime filter.

Scope: pure, in-memory, no I/O of any kind. Given an already-loaded
document sequence (e.g. core.sqlservice.vector_index.VectorIndex.documents),
builds an undirected adjacency map and answers bounded shortest-path
queries between two objects — used by core.sqlservice.context_packing to
find the shortest confirmed join path connecting two strong hybrid anchors,
never to explore the whole graph.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH",
    "RelationshipGraph",
    "build_relationship_graph",
    "find_shortest_relationship_path",
]

# Hard ceiling on max_relationship_path_length (edges) — a caller-supplied
# value above this is rejected, never silently clamped. Three edges is
# already a generous join chain (four tables); a caller wanting more is
# almost certainly asking for something this module deliberately refuses
# to be: a general graph explorer.
MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH = 3


def _relationship_neighbor_fqns_lower(metadata: Mapping[str, object]) -> tuple[str, ...]:
    """
    Every object_fqn (lowercased) one CONFIRMED relationship hop away from
    the document this metadata belongs to. Identical field/parsing
    convention to core.sqlservice.schema_context_retrieval's own
    _relationship_candidate_fqns — deliberately duplicated rather than
    imported, so this module has zero dependency on
    schema_context_retrieval (which itself depends on this module, in the
    other direction) and zero import-cycle risk.
    """
    targets = metadata.get("confirmed_relationship_targets") or ()
    sources = metadata.get("confirmed_relationship_sources") or ()
    neighbors = []
    for descriptor in (*targets, *sources):
        object_fqn, sep, _column = str(descriptor).rpartition(".")
        if sep and object_fqn:
            neighbors.append(object_fqn.lower())
    return tuple(neighbors)


@dataclass(frozen=True)
class RelationshipGraph:
    """
    Immutable undirected adjacency map over confirmed relationships only.
    Keys and values are lowercased object_fqn strings (matching this
    project's existing case-insensitive object-identity convention for
    relationship matching). `fqn_by_lower` recovers the canonical
    (original-case) spelling for any node actually present in the source
    document set.
    """

    adjacency: Mapping[str, frozenset[str]]
    fqn_by_lower: Mapping[str, str]


def build_relationship_graph(documents: Sequence) -> RelationshipGraph:
    """
    Build the graph from an already-loaded document sequence (each item
    exposing .object_fqn and .metadata — e.g.
    core.sqlservice.vector_index.IndexedSchemaDocument). Pure, in-memory,
    no I/O. An edge is added in BOTH directions regardless of which side
    (target or source) it was declared from, so shortest-path search can
    traverse a confirmed relationship from either endpoint.

    A neighbor referenced by one document's metadata but not itself part
    of `documents` (e.g. excluded by an upstream tier filter) is simply
    never added as a graph node — dangling references are silently
    dropped, never treated as reachable.
    """
    fqn_by_lower: dict[str, str] = {d.object_fqn.lower(): d.object_fqn for d in documents}
    adjacency: dict[str, set[str]] = {key: set() for key in fqn_by_lower}

    for doc in documents:
        own_key = doc.object_fqn.lower()
        for neighbor_key in _relationship_neighbor_fqns_lower(doc.metadata):
            if neighbor_key == own_key or neighbor_key not in fqn_by_lower:
                continue
            adjacency[own_key].add(neighbor_key)
            adjacency[neighbor_key].add(own_key)

    return RelationshipGraph(
        adjacency={k: frozenset(v) for k, v in adjacency.items()},
        fqn_by_lower=fqn_by_lower,
    )


def find_shortest_relationship_path(
    graph: RelationshipGraph,
    start_fqn_lower: str,
    end_fqn_lower: str,
    *,
    max_path_length: int,
) -> tuple[str, ...] | None:
    """
    Shortest confirmed-relationship path between two nodes, as a tuple of
    lowercased object_fqn keys INCLUDING both endpoints, or None if no
    path of at most `max_path_length` EDGES exists (unreachable, too far,
    or either endpoint absent from the graph — e.g. filtered out by tier
    upstream).

    Bounded BFS: never explores beyond `max_path_length` edges from
    `start_fqn_lower` — this is a targeted point-to-point search, never a
    full graph traversal. Deterministic: neighbors are visited in sorted
    order at every BFS level, so when multiple shortest paths of equal
    length exist, the lexicographically-earliest one (by each step's node
    key) is always the one returned, identically across repeated calls.

    Raises:
        ValueError: max_path_length is not a positive integer, or exceeds
            MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH.
    """
    if (
        not isinstance(max_path_length, int)
        or isinstance(max_path_length, bool)
        or max_path_length <= 0
    ):
        raise ValueError(f"max_path_length must be a positive integer, got {max_path_length!r}")
    if max_path_length > MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH:
        raise ValueError(
            f"max_path_length={max_path_length} exceeds the hard cap of "
            f"{MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH} edges"
        )

    if start_fqn_lower not in graph.adjacency or end_fqn_lower not in graph.adjacency:
        return None
    if start_fqn_lower == end_fqn_lower:
        return (start_fqn_lower,)

    visited = {start_fqn_lower}
    queue: deque[tuple[str, tuple[str, ...]]] = deque([(start_fqn_lower, (start_fqn_lower,))])

    while queue:
        current, path = queue.popleft()
        if len(path) - 1 >= max_path_length:
            continue  # would exceed the edge budget on the next hop
        for neighbor in sorted(graph.adjacency.get(current, ())):
            if neighbor in visited:
                continue
            new_path = path + (neighbor,)
            if neighbor == end_fqn_lower:
                return new_path
            visited.add(neighbor)
            queue.append((neighbor, new_path))

    return None
