"""
Graph utilities for the Dynamic Tool Creation Engine.

Responsibilities:
  - Build adjacency map from edges
  - Produce a topological (dependency-safe) execution order
  - Detect and assert absence of cycles
  - Query direct upstream / downstream neighbours

Does NOT execute nodes, resolve templates, or write to any store.

Imports only from stdlib, core.engine.contracts, and core.engine.schema.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Iterator, List, Tuple

from core.engine.contracts import ActionNode, CycleDetectedError, ExecutionGraph
from core.engine.schema import validate_execution_graph


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_adjacency(graph: ExecutionGraph) -> Dict[str, List[str]]:
    """
    Build a successor adjacency map from graph.edges.

    Every node in graph.nodes appears as a key, even nodes with no outgoing
    edges, so callers can iterate all nodes safely without key-existence checks.

    Returns: {node_id: [direct_successor_id, ...]}
    """
    adj: Dict[str, List[str]] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        adj[edge.from_node_id].append(edge.to_node_id)
    return adj


def get_execution_order(graph: ExecutionGraph) -> List[ActionNode]:
    """
    Return ActionNode objects in topological (dependency-safe) execution order.

    Uses Kahn's algorithm (BFS-based). Nodes whose dependencies have all
    completed come before nodes that depend on them. Among nodes with equal
    in-degree, original position in graph.nodes is preserved for determinism.

    Raises SchemaValidationError if the graph structure is invalid.
    Raises CycleDetectedError if the graph contains a cycle.
    """
    validate_execution_graph(graph)
    assert_acyclic(graph)

    node_by_id: Dict[str, ActionNode] = {node.id: node for node in graph.nodes}
    adj = build_adjacency(graph)

    in_degree: Dict[str, int] = {node.id: 0 for node in graph.nodes}
    for successors in adj.values():
        for successor in successors:
            in_degree[successor] += 1

    # Seed with all roots (in-degree == 0), respecting graph.nodes order.
    queue: deque[str] = deque(
        node.id for node in graph.nodes if in_degree[node.id] == 0
    )

    result: List[ActionNode] = []
    while queue:
        node_id = queue.popleft()
        result.append(node_by_id[node_id])
        for successor in adj[node_id]:
            in_degree[successor] -= 1
            if in_degree[successor] == 0:
                queue.append(successor)

    # assert_acyclic already guards this; kept as a hard invariant.
    if len(result) != len(graph.nodes):
        raise CycleDetectedError(
            "Topological sort did not visit all nodes — graph may contain a cycle"
        )

    return result


def detect_cycle(graph: ExecutionGraph) -> bool:
    """
    Return True if the execution graph contains at least one cycle.

    Uses iterative three-colour DFS to avoid Python recursion-depth limits
    on large graphs.

    Colour codes:
      0 = unvisited
      1 = in the current DFS stack (grey)
      2 = fully processed (black)
    """
    adj = build_adjacency(graph)
    color: Dict[str, int] = {node.id: 0 for node in graph.nodes}

    for start in graph.nodes:
        if color[start.id] != 0:
            continue

        # Stack entries: (node_id, iterator over that node's successors)
        stack: List[Tuple[str, Iterator[str]]] = [
            (start.id, iter(adj[start.id]))
        ]
        color[start.id] = 1

        while stack:
            node_id, neighbours = stack[-1]
            try:
                neighbour = next(neighbours)
                if color.get(neighbour, 0) == 1:
                    return True  # Back edge → cycle
                if color.get(neighbour, 0) == 0:
                    color[neighbour] = 1
                    stack.append((neighbour, iter(adj.get(neighbour, []))))
            except StopIteration:
                color[node_id] = 2
                stack.pop()

    return False


def assert_acyclic(graph: ExecutionGraph) -> None:
    """Raise CycleDetectedError if the graph contains a cycle."""
    if detect_cycle(graph):
        raise CycleDetectedError(
            "Execution graph contains a cycle — all tool graphs must be acyclic"
        )


def get_downstream_nodes(graph: ExecutionGraph, node_id: str) -> List[str]:
    """Return the direct downstream node ids (immediate successors via edges)."""
    return [edge.to_node_id for edge in graph.edges if edge.from_node_id == node_id]


def get_upstream_nodes(graph: ExecutionGraph, node_id: str) -> List[str]:
    """Return the direct upstream node ids (immediate predecessors via edges)."""
    return [edge.from_node_id for edge in graph.edges if edge.to_node_id == node_id]
