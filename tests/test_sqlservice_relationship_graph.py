"""
Focused unit tests — SQL rebuild Phase 5, Step 5: in-memory confirmed-
relationship graph and bounded shortest-path lookup
(core/sqlservice/relationship_graph.py).

Pure, in-memory, no SQLite, no OpenAI, no vector/lexical scoring anywhere
in this file — every document fixture is hand-constructed.

Run:
    venv/Scripts/pytest tests/test_sqlservice_relationship_graph.py -v
"""

from types import MappingProxyType

import pytest

from core.sqlservice.relationship_graph import (
    MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH,
    build_relationship_graph,
    find_shortest_relationship_path,
)


class _Doc:
    def __init__(self, object_fqn, *, tier=1, metadata=None):
        self.object_fqn = object_fqn
        self.retrieval_tier = tier
        self.metadata = MappingProxyType(metadata or {})


def _rel_doc(object_fqn, *, tier=1, targets=(), sources=(), inferred_targets=()):
    metadata = {}
    if targets:
        metadata["confirmed_relationship_targets"] = tuple(targets)
    if sources:
        metadata["confirmed_relationship_sources"] = tuple(sources)
    if inferred_targets:
        metadata["inferred_relationship_targets"] = tuple(inferred_targets)
    return _Doc(object_fqn, tier=tier, metadata=metadata)


# ── Graph construction ──────────────────────────────────────────────────

def test_graph_builds_undirected_edges_from_outbound_targets():
    docs = [_rel_doc("dbo.a", targets=("dbo.b.id",)), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    assert "dbo.b" in graph.adjacency["dbo.a"]
    assert "dbo.a" in graph.adjacency["dbo.b"]  # reachable from either side


def test_graph_builds_edges_from_inbound_sources_too():
    docs = [_rel_doc("dbo.a", sources=("dbo.b.id",)), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    assert "dbo.b" in graph.adjacency["dbo.a"]
    assert "dbo.a" in graph.adjacency["dbo.b"]


def test_dangling_reference_not_added_as_a_node():
    docs = [_rel_doc("dbo.a", targets=("dbo.nonexistent.id",))]
    graph = build_relationship_graph(docs)
    assert "dbo.nonexistent" not in graph.adjacency
    assert graph.adjacency["dbo.a"] == frozenset()


def test_no_self_loop():
    docs = [_rel_doc("dbo.a", targets=("dbo.a.id",))]
    graph = build_relationship_graph(docs)
    assert graph.adjacency["dbo.a"] == frozenset()


def test_inferred_relationships_never_become_graph_edges():
    """Requirement: never use inferred relationships by default. The graph
    builder has no code path that reads inferred_relationship_targets/
    sources at all — proven by a document whose ONLY relationship signal
    is inferred."""
    docs = [_rel_doc("dbo.a", inferred_targets=("dbo.b.id",)), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    assert graph.adjacency["dbo.a"] == frozenset()
    assert graph.adjacency["dbo.b"] == frozenset()


def test_fqn_by_lower_recovers_canonical_case():
    docs = [_rel_doc("dbo.MyTable")]
    graph = build_relationship_graph(docs)
    assert graph.fqn_by_lower["dbo.mytable"] == "dbo.MyTable"


# ── Shortest-path lookup ────────────────────────────────────────────────

def test_direct_one_edge_path():
    docs = [_rel_doc("dbo.a", targets=("dbo.b.id",)), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    path = find_shortest_relationship_path(graph, "dbo.a", "dbo.b", max_path_length=2)
    assert path == ("dbo.a", "dbo.b")


def test_two_edge_path_via_one_bridge():
    docs = [
        _rel_doc("dbo.a", targets=("dbo.bridge.id",)),
        _rel_doc("dbo.bridge", targets=("dbo.c.id",)),
        _rel_doc("dbo.c"),
    ]
    graph = build_relationship_graph(docs)
    path = find_shortest_relationship_path(graph, "dbo.a", "dbo.c", max_path_length=2)
    assert path == ("dbo.a", "dbo.bridge", "dbo.c")


def test_three_edge_path_via_two_bridges():
    docs = [
        _rel_doc("dbo.a", targets=("dbo.b1.id",)),
        _rel_doc("dbo.b1", targets=("dbo.b2.id",)),
        _rel_doc("dbo.b2", targets=("dbo.z.id",)),
        _rel_doc("dbo.z"),
    ]
    graph = build_relationship_graph(docs)
    path = find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=3)
    assert path == ("dbo.a", "dbo.b1", "dbo.b2", "dbo.z")


def test_path_exceeding_max_length_returns_none():
    docs = [
        _rel_doc("dbo.a", targets=("dbo.b1.id",)),
        _rel_doc("dbo.b1", targets=("dbo.b2.id",)),
        _rel_doc("dbo.b2", targets=("dbo.z.id",)),
        _rel_doc("dbo.z"),
    ]
    graph = build_relationship_graph(docs)
    # The real path is 3 edges; asking for at most 2 must fail closed.
    assert find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=2) is None


def test_shortest_path_preferred_over_longer_alternative():
    # a-z direct edge AND a longer a-b-z detour both exist — shortest wins.
    docs = [
        _rel_doc("dbo.a", targets=("dbo.z.id", "dbo.b.id")),
        _rel_doc("dbo.b", targets=("dbo.z.id",)),
        _rel_doc("dbo.z"),
    ]
    graph = build_relationship_graph(docs)
    path = find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=3)
    assert path == ("dbo.a", "dbo.z")


def test_disconnected_nodes_return_none_safely():
    docs = [_rel_doc("dbo.a"), _rel_doc("dbo.isolated")]
    graph = build_relationship_graph(docs)
    assert find_shortest_relationship_path(graph, "dbo.a", "dbo.isolated", max_path_length=3) is None


def test_start_or_end_absent_from_graph_returns_none():
    docs = [_rel_doc("dbo.a")]
    graph = build_relationship_graph(docs)
    assert find_shortest_relationship_path(graph, "dbo.a", "dbo.never_existed", max_path_length=3) is None


def test_same_start_and_end_returns_trivial_single_node_path():
    docs = [_rel_doc("dbo.a")]
    graph = build_relationship_graph(docs)
    assert find_shortest_relationship_path(graph, "dbo.a", "dbo.a", max_path_length=3) == ("dbo.a",)


def test_deterministic_tie_break_among_equal_length_paths():
    # Two DIFFERENT 2-edge routes from a to z (via bridge_x or bridge_y) —
    # the lexicographically-earlier neighbor at each BFS level always wins,
    # identically across repeated calls.
    docs = [
        _rel_doc("dbo.a", targets=("dbo.bridge_y.id", "dbo.bridge_x.id")),
        _rel_doc("dbo.bridge_x", targets=("dbo.z.id",)),
        _rel_doc("dbo.bridge_y", targets=("dbo.z.id",)),
        _rel_doc("dbo.z"),
    ]
    graph = build_relationship_graph(docs)
    first = find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=2)
    second = find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=2)
    assert first == second == ("dbo.a", "dbo.bridge_x", "dbo.z")  # "bridge_x" < "bridge_y"


def test_tier_filtering_is_the_callers_responsibility_via_graph_membership():
    """The graph itself has no notion of allowed_tiers — the caller (see
    schema_context_retrieval.py) builds it only from tier-allowed
    documents in the first place, so a disallowed-tier node is simply
    never a graph member and can never appear on any path."""
    docs = [
        _rel_doc("dbo.a", targets=("dbo.archived_bridge.id",), tier=1),
        _rel_doc("dbo.archived_bridge", targets=("dbo.z.id",), tier=4),
        _rel_doc("dbo.z", tier=1),
    ]
    tier_allowed_only = [d for d in docs if d.retrieval_tier in (1, 2)]
    graph = build_relationship_graph(tier_allowed_only)
    assert find_shortest_relationship_path(graph, "dbo.a", "dbo.z", max_path_length=3) is None


# ── Validation ──────────────────────────────────────────────────────────

def test_invalid_max_path_length_rejected():
    docs = [_rel_doc("dbo.a"), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    with pytest.raises(ValueError):
        find_shortest_relationship_path(graph, "dbo.a", "dbo.b", max_path_length=0)
    with pytest.raises(ValueError):
        find_shortest_relationship_path(graph, "dbo.a", "dbo.b", max_path_length=-1)


def test_max_path_length_hard_cap_enforced():
    docs = [_rel_doc("dbo.a"), _rel_doc("dbo.b")]
    graph = build_relationship_graph(docs)
    assert MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH == 3
    with pytest.raises(ValueError):
        find_shortest_relationship_path(
            graph, "dbo.a", "dbo.b", max_path_length=MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH + 1,
        )
    # Exactly the cap is allowed (does not itself raise).
    find_shortest_relationship_path(graph, "dbo.a", "dbo.b", max_path_length=MAX_ALLOWED_RELATIONSHIP_PATH_LENGTH)


# ── No domain-specific vocabulary ──────────────────────────────────────

def test_no_domain_specific_tokens_in_production_module():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "core" / "sqlservice" / "relationship_graph.py").read_text(encoding="utf-8")
    for banned in ("EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "LMS_SUBSCRIPTION_PAYMENTS", "exampleco", "EXAMPLECO"):
        assert banned not in source
