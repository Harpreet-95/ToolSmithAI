"""
Focused unit tests — SQL Architecture Correction, Phase 6, Step 3:
deterministic join-subgraph planner (core/sqlservice/join_planner.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Small synthetic fixtures only. Most tests build a
SchemaLinkingResult directly (bypassing core.sqlservice.schema_linker's own
question-matching heuristics) so a planner-test failure is never confused
with a linker-matching failure; a few end-to-end tests go through the real
link_question_to_schema()/build_semantic_catalog() pipeline for integration
confidence.

Run:
    venv/Scripts/pytest tests/test_sqlservice_join_planner.py -v
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from core.sqlservice.schema_linker import (
    AnchorCandidate,
    MatchedRequirement,
    QuestionRequirements,
    RequirementCandidate,
    SchemaLinkingResult,
    link_question_to_schema,
)
from core.sqlservice.semantic_catalog import build_semantic_catalog as build_catalog
from core.sqlservice.join_planner import (
    DIRECTION_FORWARD,
    DIRECTION_REVERSE,
    JOIN_CAPABILITY_JOINABLE,
    JOIN_CAPABILITY_LINEAGE_ONLY,
    JOIN_CAPABILITY_UNUSABLE,
    LIMIT_MAX_ALTERNATIVE_PLANS,
    LIMIT_MAX_PATH_HOPS,
    LIMIT_MAX_SELECTED_OBJECTS,
    LIMIT_MAX_TERMINAL_CANDIDATES,
    RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY,
    RELATIONSHIP_TYPE_PHYSICAL_FK,
    STATUS_INDEX_BUILD_FAILED,
    STATUS_LIMITS_EXCEEDED,
    STATUS_NOT_PREPARED,
    STATUS_NO_CANDIDATES,
    STATUS_NO_REQUIREMENTS,
    STATUS_OK,
    STATUS_PARTIAL_COVERAGE,
    DEFAULT_JOIN_PLANNING_LIMITS,
    JoinPlanningLimits,
    PlannedJoinEdge,
    PlanningIndexPreparationResult,
    SchemaSubgraphPlan,
    clear_join_planner_cache,
    join_planner_cache_size,
    plan_schema_join_subgraph,
    prepare_schema_planning_indexes,
)
import core.sqlservice.join_planner as join_planner_module

# ── Fixture builders ────────────────────────────────────────────────────────

_DEFAULT_META = {
    "business_name": None, "description": None, "domain": None, "synonyms": (),
    "column_names": (), "column_descriptors": (), "has_primary_key": False,
    "primary_key_columns": (), "foreign_key_targets": (),
    "confirmed_relationship_targets": (), "confirmed_relationship_sources": (),
    "confirmed_relationship_edges": (), "inferred_relationship_targets": (),
    "inferred_relationship_sources": (), "status_columns": (), "date_columns": (), "measure_columns": (),
}


class _Doc:
    def __init__(self, object_fqn: str, object_type: str = "TABLE", retrieval_tier: int = 1, **meta_overrides):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = retrieval_tier
        merged = dict(_DEFAULT_META)
        merged.update(meta_overrides)
        self.metadata = MappingProxyType(merged)


def _out(column: str, other: str, kind: str = RELATIONSHIP_TYPE_PHYSICAL_FK) -> dict:
    return {"direction": "outbound", "column": column, "other": other, "kind": kind}


def _in(column: str, other: str, kind: str = RELATIONSHIP_TYPE_PHYSICAL_FK) -> dict:
    return {"direction": "inbound", "column": column, "other": other, "kind": kind}


def _req_candidate(object_fqn: str, confidence: float) -> RequirementCandidate:
    return RequirementCandidate(
        object_fqn=object_fqn, column_name=None, technical_label="x", match_kind="COLUMN_TOKEN",
        confidence=confidence, raw_confidence=confidence, informativeness=1.0, evidence=(),
    )


# Step 3C default strength per concept_kind — mirrors the pre-Step-3C
# kind-only role mapping exactly, so every EXISTING test (written before
# schema_linker computed its own strength) keeps behaving identically;
# tests that specifically exercise strength/role interaction pass
# `strength=` explicitly to override this default.
_DEFAULT_STRENGTH_BY_KIND = {
    "ENTITY": "CORE", "MEASURE": "CORE", "DIMENSION": "CONSTRAINT",
    "DATE_ROLE": "CONSTRAINT", "IDENTIFIER": "SUPPORTING",
}


def _requirement(concept_key: str, candidates: list[tuple[str, float]], strength: str | None = None) -> MatchedRequirement:
    kind, _, label = concept_key.partition(":")
    cands = tuple(_req_candidate(fqn, conf) for fqn, conf in candidates)
    if strength is None:
        strength = _DEFAULT_STRENGTH_BY_KIND.get(kind, "SUPPORTING")
    return MatchedRequirement(
        concept_kind=kind, concept_key=concept_key, normalized_label=label, matched_terms=(label,),
        candidates=cands, candidate_total_count=len(cands),
        confidence=cands[0].confidence if cands else 0.0, ambiguous=False, evidence=(), strength=strength,
    )


def _linking_result(
    requirements: list[MatchedRequirement], anchor_fqns: tuple[str, ...] = (),
    anchor_scores: dict[str, float] | None = None,
) -> SchemaLinkingResult:
    by_kind: dict[str, list[MatchedRequirement]] = {
        "ENTITY": [], "MEASURE": [], "DIMENSION": [], "DATE_ROLE": [], "IDENTIFIER": [],
    }
    for r in requirements:
        by_kind[r.concept_kind].append(r)
    qr = QuestionRequirements(
        operation="UNKNOWN", entity_concepts=tuple(by_kind["ENTITY"]), measure_concepts=tuple(by_kind["MEASURE"]),
        dimension_concepts=tuple(by_kind["DIMENSION"]), date_role_concepts=tuple(by_kind["DATE_ROLE"]),
        identifier_concepts=tuple(by_kind["IDENTIFIER"]), wants_comparison=False, wants_trend=False,
        wants_grouping=False, unmatched_terms=(), unresolved_filter_values=(), ambiguity_signals=(),
    )
    anchor_scores = anchor_scores or {}
    anchors = tuple(
        AnchorCandidate(
            object_fqn=fqn, object_type="TABLE", retrieval_tier=1, supporting_concepts=(), supporting_columns=(),
            semantic_score=None, lexical_score=None, catalog_score=0.5, example_score=0.0, canonical_score=0.5,
            model_semantic_score=0.0,
            combined_score=anchor_scores.get(fqn, 0.5), evidence=(), confidence=anchor_scores.get(fqn, 0.5),
            admission_reasons=("CONCEPT_MATCH",),
        )
        for fqn in anchor_fqns
    )
    return SchemaLinkingResult(
        status="OK", requirements=qr, anchors=anchors, ambiguities=(), warnings=(), fingerprint="fp",
        eligible_object_count=0, considered_object_count=0, anchor_total_count=len(anchors), elapsed_ms=0.0,
        relevance_qualified_object_count=0, rejected_for_no_relevance_count=0, anchor_selected_count=len(anchors),
        anchors_rejected_by_confidence=0, anchor_ceiling_reached=False, no_confident_anchor_reason=None,
        raw_catalog_match_count=0, normalized_requirement_count=len(requirements), duplicate_matches_merged_count=0,
        common_token_matches_downweighted_count=0, candidate_alternatives_retained_count=0,
        anchors_selected_by_independent_evidence_count=0, anchors_retained_for_ambiguity_count=0,
    )


@pytest.fixture(autouse=True)
def _isolated_cache():
    clear_join_planner_cache()
    yield
    clear_join_planner_cache()


def _plan(requirements, docs, anchor_fqns=(), limits=None, anchor_scores=None):
    link = _linking_result(requirements, anchor_fqns, anchor_scores)
    kwargs = {"limits": limits} if limits is not None else {}
    return plan_schema_join_subgraph(link, docs, **kwargs)


# ── 1. Single-table complete plan ──────────────────────────────────────────


def test_single_object_covers_all_requirements():
    docs = [_Doc("dbo.Customers", column_names=("CustomerID", "Status"))]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("DIMENSION:status", [("dbo.Customers", 0.8)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert result.primary_plan.selected_object_fqns == ("dbo.Customers",)
    assert result.primary_plan.join_edges == ()
    assert result.primary_plan.uncovered_requirement_keys == ()


# ── 2. Direct two-table join ────────────────────────────────────────────────


def test_direct_two_table_join():
    customers = _Doc("dbo.Customers", column_names=("CustomerID",),
                      confirmed_relationship_sources=("dbo.Orders.CustomerID",),
                      confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),))
    orders = _Doc("dbo.Orders", column_names=("OrderID", "CustomerID"),
                   confirmed_relationship_targets=("dbo.Customers.CustomerID",),
                   confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),))
    docs = [customers, orders]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders"}
    assert len(result.primary_plan.join_edges) == 1
    edge = result.primary_plan.join_edges[0]
    assert edge.join_capability == JOIN_CAPABILITY_JOINABLE
    assert edge.column_pairs == (("CustomerID", "CustomerID"),)
    assert result.primary_plan.bridge_object_fqns == ()


# ── 3. Composite-key join ───────────────────────────────────────────────────


def test_composite_key_join_grouped_into_one_edge():
    orders = _Doc("dbo.Orders", column_names=("OrderID", "LineID"),
                   confirmed_relationship_sources=("dbo.OrderLines.OrderID", "dbo.OrderLines.LineID"),
                   confirmed_relationship_edges=(
                       _in("OrderID", "dbo.OrderLines.OrderID"),
                       _in("LineID", "dbo.OrderLines.LineID"),
                   ))
    order_lines = _Doc("dbo.OrderLines", column_names=("OrderID", "LineID"),
                        confirmed_relationship_targets=("dbo.Orders.OrderID", "dbo.Orders.LineID"),
                        confirmed_relationship_edges=(
                            _out("OrderID", "dbo.Orders.OrderID"),
                            _out("LineID", "dbo.Orders.LineID"),
                        ))
    docs = [orders, order_lines]
    reqs = [
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
        _requirement("ENTITY:orderline", [("dbo.OrderLines", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert len(result.primary_plan.join_edges) == 1
    edge = result.primary_plan.join_edges[0]
    assert edge.column_pairs == (("LineID", "LineID"), ("OrderID", "OrderID"))


# ── 4/5. Three-table path with a bridge that has zero question relevance ──


def _bridge_schema():
    customers = _Doc("dbo.Customers", column_names=("CustomerID",),
                      confirmed_relationship_sources=("dbo.Orders.CustomerID",),
                      confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),))
    orders = _Doc("dbo.Orders", column_names=("OrderID", "CustomerID", "ProductID"),
                   confirmed_relationship_targets=("dbo.Customers.CustomerID",),
                   confirmed_relationship_sources=("dbo.Products.ProductID",),
                   confirmed_relationship_edges=(
                       _out("CustomerID", "dbo.Customers.CustomerID"),
                       _in("ProductID", "dbo.Products.ProductID"),
                   ))
    products = _Doc("dbo.Products", column_names=("ProductID",),
                     confirmed_relationship_targets=("dbo.Orders.ProductID",),
                     confirmed_relationship_edges=(_out("ProductID", "dbo.Orders.ProductID"),))
    return customers, orders, products


def test_three_table_path_with_bridge_of_zero_question_relevance():
    customers, orders, products = _bridge_schema()
    docs = [customers, orders, products]
    # Orders is a candidate for NEITHER requirement — pure bridge.
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:product", [("dbo.Products", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders", "dbo.Products"}
    assert result.primary_plan.bridge_object_fqns == ("dbo.Orders",)
    assert len(result.primary_plan.join_edges) == 2


# ── 6/7. Alternative terminal candidates: connectivity beats raw confidence ─


def test_lower_confidence_but_connected_candidate_wins_over_disconnected_higher_confidence_one():
    # Requirement A has two candidates: HighConf (isolated, no edges) and
    # LowConf (connects directly to requirement B's only candidate).
    high_conf = _Doc("dbo.HighConf", column_names=("Id",))
    low_conf = _Doc("dbo.LowConf", column_names=("Id", "BID"),
                     confirmed_relationship_targets=("dbo.BTable.Id",),
                     confirmed_relationship_edges=(_out("BID", "dbo.BTable.Id"),))
    b_table = _Doc("dbo.BTable", column_names=("Id",),
                    confirmed_relationship_sources=("dbo.LowConf.BID",),
                    confirmed_relationship_edges=(_in("Id", "dbo.LowConf.BID"),))
    docs = [high_conf, low_conf, b_table]
    reqs = [
        _requirement("ENTITY:a", [("dbo.HighConf", 0.95), ("dbo.LowConf", 0.4)]),
        _requirement("ENTITY:b", [("dbo.BTable", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert "dbo.LowConf" in result.primary_plan.selected_object_fqns
    assert "dbo.HighConf" not in result.primary_plan.selected_object_fqns


# ── 8. Coverage beats shorter incomplete plan ──────────────────────────────


def test_full_coverage_beats_smaller_partial_plan():
    customers, orders, products = _bridge_schema()
    docs = [customers, orders, products]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.99)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.5)]),
        _requirement("ENTITY:product", [("dbo.Products", 0.5)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    # The 1-object plan {Customers} alone would score high on "relevance"
    # and be trivially small, but must lose to the full 3-object connected
    # plan on the coverage tier.
    assert len(result.primary_plan.selected_object_fqns) == 3
    assert result.primary_plan.covered_requirement_keys == ("ENTITY:customer", "ENTITY:order", "ENTITY:product")


# ── 9. Smallest complete connected subgraph (no unnecessary detour) ───────


def test_smallest_complete_subgraph_avoids_unnecessary_detour():
    # A-B direct edge; A-C-B also exists (C is an unnecessary detour).
    a = _Doc("dbo.A", column_names=("Id", "BID", "CID"),
             confirmed_relationship_targets=("dbo.B.Id", "dbo.C.Id"),
             confirmed_relationship_edges=(_out("BID", "dbo.B.Id"), _out("CID", "dbo.C.Id")))
    b = _Doc("dbo.B", column_names=("Id",),
             confirmed_relationship_sources=("dbo.A.BID", "dbo.C.BID"),
             confirmed_relationship_edges=(_in("Id", "dbo.A.BID"), _in("Id", "dbo.C.BID")))
    c = _Doc("dbo.C", column_names=("Id", "BID"),
             confirmed_relationship_sources=("dbo.A.CID",), confirmed_relationship_targets=("dbo.B.Id",),
             confirmed_relationship_edges=(_in("Id", "dbo.A.CID"), _out("BID", "dbo.B.Id")))
    docs = [a, b, c]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert result.primary_plan.selected_object_fqns == ("dbo.A", "dbo.B")
    assert "dbo.C" not in result.primary_plan.selected_object_fqns


# ── 10. Four-requirement bounded plan ──────────────────────────────────────


def test_four_requirement_chain_plan():
    a = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_sources=("dbo.B.AID",),
             confirmed_relationship_edges=(_in("Id", "dbo.B.AID"),))
    b = _Doc("dbo.B", column_names=("Id", "AID"), confirmed_relationship_targets=("dbo.A.Id",),
             confirmed_relationship_sources=("dbo.C.BID",),
             confirmed_relationship_edges=(_out("AID", "dbo.A.Id"), _in("Id", "dbo.C.BID")))
    c = _Doc("dbo.C", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.B.Id",),
             confirmed_relationship_sources=("dbo.D.CID",),
             confirmed_relationship_edges=(_out("BID", "dbo.B.Id"), _in("Id", "dbo.D.CID")))
    d = _Doc("dbo.D", column_names=("Id", "CID"), confirmed_relationship_targets=("dbo.C.Id",),
              confirmed_relationship_edges=(_out("CID", "dbo.C.Id"),))
    docs = [a, b, c, d]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)]),
        _requirement("ENTITY:c", [("dbo.C", 0.9)]), _requirement("ENTITY:d", [("dbo.D", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.A", "dbo.B", "dbo.C", "dbo.D"}
    assert len(result.primary_plan.join_edges) == 3


# ── 11. Cycle prevention / duplicate-path suppression ─────────────────────


def test_cycle_prevention_produces_a_spanning_tree_not_a_cycle():
    # Triangle: A-B, B-C, A-C all directly joinable.
    a = _Doc("dbo.A", column_names=("Id", "BID", "CID"),
             confirmed_relationship_targets=("dbo.B.Id", "dbo.C.Id"),
             confirmed_relationship_edges=(_out("BID", "dbo.B.Id"), _out("CID", "dbo.C.Id")))
    b = _Doc("dbo.B", column_names=("Id", "CID"),
             confirmed_relationship_sources=("dbo.A.BID",), confirmed_relationship_targets=("dbo.C.Id",),
             confirmed_relationship_edges=(_in("Id", "dbo.A.BID"), _out("CID", "dbo.C.Id")))
    c = _Doc("dbo.C", column_names=("Id",),
             confirmed_relationship_sources=("dbo.A.CID", "dbo.B.CID"),
             confirmed_relationship_edges=(_in("Id", "dbo.A.CID"), _in("Id", "dbo.B.CID")))
    docs = [a, b, c]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)]),
        _requirement("ENTITY:c", [("dbo.C", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert len(result.primary_plan.selected_object_fqns) == 3
    # A spanning tree over 3 nodes has exactly 2 edges — never all 3.
    assert len(result.primary_plan.join_edges) == 2
    pairs = {(e.from_object_fqn, e.to_object_fqn) for e in result.primary_plan.join_edges}
    assert len(pairs) == 2  # no duplicate edge


# ── 12. Deterministic tie-breaking ─────────────────────────────────────────


def test_deterministic_tie_break_prefers_lexicographically_smaller_object():
    alpha = _Doc("dbo.Alpha", column_names=("Id",))
    beta = _Doc("dbo.Beta", column_names=("Id",))
    docs = [alpha, beta]
    reqs = [_requirement("ENTITY:thing", [("dbo.Alpha", 0.8), ("dbo.Beta", 0.8)])]
    results = {_plan(reqs, docs).primary_plan.selected_object_fqns for _ in range(5)}
    assert results == {("dbo.Alpha",)}


# ── 13/14. Duplicate edge suppression + reversed relationship direction ───


def test_symmetric_outbound_and_inbound_records_collapse_to_one_edge():
    # The normal case: both endpoints record the SAME physical edge.
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.B.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.B.Id"),))
    b = _Doc("dbo.B", column_names=("Id",), confirmed_relationship_sources=("dbo.A.BID",),
             confirmed_relationship_edges=(_in("Id", "dbo.A.BID"),))
    docs = [a, b]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result = _plan(reqs, docs)
    assert len(result.primary_plan.join_edges) == 1


def test_reversed_relationship_direction_produces_identical_canonical_edge():
    # Case 1: only the FK owner (B) records the outbound edge.
    a1 = _Doc("dbo.A", column_names=("Id",))
    b1 = _Doc("dbo.B", column_names=("Id", "AID"), confirmed_relationship_targets=("dbo.A.Id",),
              confirmed_relationship_edges=(_out("AID", "dbo.A.Id"),))
    docs1 = [a1, b1]

    # Case 2: only the referenced side (A) records the SAME physical edge
    # as an inbound entry.
    a2 = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_sources=("dbo.B.AID",),
              confirmed_relationship_edges=(_in("Id", "dbo.B.AID"),))
    b2 = _Doc("dbo.B", column_names=("Id", "AID"))
    docs2 = [a2, b2]

    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result1 = _plan(reqs, docs1)
    clear_join_planner_cache()
    result2 = _plan(reqs, docs2)

    edge1 = result1.primary_plan.join_edges[0]
    edge2 = result2.primary_plan.join_edges[0]
    assert (edge1.from_object_fqn, edge1.to_object_fqn, edge1.column_pairs, edge1.direction) == \
           (edge2.from_object_fqn, edge2.to_object_fqn, edge2.column_pairs, edge2.direction)
    assert edge1.from_object_fqn == "dbo.A" and edge1.to_object_fqn == "dbo.B"
    assert edge1.direction == DIRECTION_REVERSE  # B (the "to" side) owns the FK


# ── 15. Tier 3/4 bridge exclusion ──────────────────────────────────────────


def test_tier_3_and_4_objects_never_become_bridges():
    a = _Doc("dbo.A", column_names=("Id", "MID"), confirmed_relationship_targets=("dbo.Middle.Id",),
             confirmed_relationship_edges=(_out("MID", "dbo.Middle.Id"),))
    middle_tier4 = _Doc("dbo.Middle", retrieval_tier=4, column_names=("Id", "CID"),
                         confirmed_relationship_sources=("dbo.A.MID",), confirmed_relationship_targets=("dbo.C.Id",),
                         confirmed_relationship_edges=(_in("Id", "dbo.A.MID"), _out("CID", "dbo.C.Id")))
    c = _Doc("dbo.C", column_names=("Id",), confirmed_relationship_sources=("dbo.Middle.CID",),
             confirmed_relationship_edges=(_in("Id", "dbo.Middle.CID"),))
    docs = [a, middle_tier4, c]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:c", [("dbo.C", 0.9)])]
    result = _plan(reqs, docs)
    # No path exists once Tier 4 is excluded from the graph entirely.
    assert result.status == STATUS_PARTIAL_COVERAGE
    assert "dbo.Middle" not in (result.primary_plan.selected_object_fqns if result.primary_plan else ())


# ── 16. Unknown/missing column + missing object edge rejection ────────────


def test_edge_referencing_unknown_column_is_unusable():
    a = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_targets=("dbo.B.NoSuchColumn",),
             confirmed_relationship_edges=(_out("Id", "dbo.B.NoSuchColumn"),))
    b = _Doc("dbo.B", column_names=("Id",))
    docs = [a, b]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_PARTIAL_COVERAGE
    assert result.primary_plan.join_edges == ()


def test_edge_referencing_missing_object_is_unusable():
    a = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_targets=("dbo.Nonexistent.Id",),
             confirmed_relationship_edges=(_out("Id", "dbo.Nonexistent.Id"),))
    docs = [a]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK  # single object, no join needed at all
    assert result.primary_plan.join_edges == ()


def test_self_loop_edge_is_unusable():
    a = _Doc("dbo.A", column_names=("Id", "ParentId"), confirmed_relationship_targets=("dbo.A.Id",),
             confirmed_relationship_edges=(_out("ParentId", "dbo.A.Id"),))
    docs = [a]
    from core.sqlservice.join_planner import _build_join_graph_index
    index = _build_join_graph_index(docs)
    assert index.joinable_edges == ()
    assert index.unusable_edge_count == 1


# ── 17/18. "*" view-dependency is lineage-only; never an ON predicate ─────


def test_star_view_dependency_is_lineage_only_never_joinable():
    from core.sqlservice.join_planner import _build_join_graph_index
    base = _Doc("dbo.Base", column_names=("Id",), confirmed_relationship_sources=("dbo.View1.*",),
                confirmed_relationship_edges=(_in("*", "dbo.View1.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    view = _Doc("dbo.View1", object_type="VIEW", column_names=("Id",),
                confirmed_relationship_targets=("dbo.Base.*",),
                confirmed_relationship_edges=(_out("*", "dbo.Base.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    index = _build_join_graph_index([base, view])
    assert index.joinable_edges == ()
    assert len(index.lineage_edges) == 1
    assert index.lineage_edges[0].join_capability == JOIN_CAPABILITY_LINEAGE_ONLY
    assert index.joinable_adjacency["dbo.base"] == ()
    assert index.joinable_adjacency["dbo.view1"] == ()


def test_lineage_only_relationship_cannot_connect_two_requirements():
    base = _Doc("dbo.Base", column_names=("Id",), confirmed_relationship_sources=("dbo.View1.*",),
                confirmed_relationship_edges=(_in("*", "dbo.View1.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    view = _Doc("dbo.View1", object_type="VIEW", column_names=("Id",),
                confirmed_relationship_targets=("dbo.Base.*",),
                confirmed_relationship_edges=(_out("*", "dbo.Base.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    docs = [base, view]
    reqs = [_requirement("ENTITY:base", [("dbo.Base", 0.9)]), _requirement("ENTITY:view", [("dbo.View1", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_PARTIAL_COVERAGE
    assert set(result.primary_plan.uncovered_requirement_keys) & {"ENTITY:base", "ENTITY:view"}


# ── Step 5A regression: a lineage-only edge whose OTHER endpoint was
# excluded from `documents` (e.g. a Tier 3/4 object a caller like
# core.sqlservice.schema_plan_options never loads at all, since it only
# requests tiers=(1, 2)) used to crash _group_into_planned_edges with a
# KeyError, because _classify_raw_edge_capability returned LINEAGE_ONLY for
# the "*"/CATALOG_VIEW_DEPENDENCY branch BEFORE checking that both
# endpoints exist in fqn_by_lower — unlike every other branch, which
# already did that check. Every test above this point happens to always
# pass BOTH lineage-edge endpoints into `documents`, so this gap was never
# exercised. These pass only ONE endpoint, reproducing the real defect.


def test_lineage_only_edge_with_missing_source_endpoint_is_unusable_not_a_crash():
    from core.sqlservice.join_planner import _build_join_graph_index
    base = _Doc("dbo.Base", column_names=("Id",), confirmed_relationship_sources=("dbo.View1.*",),
                confirmed_relationship_edges=(_in("*", "dbo.View1.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    # "dbo.View1" (the edge's other endpoint) is deliberately never included.
    index = _build_join_graph_index([base])  # must not raise KeyError
    assert index.joinable_edges == ()
    assert index.lineage_edges == ()
    assert index.unusable_edge_count == 1
    assert index.joinable_adjacency["dbo.base"] == ()
    assert "dbo.view1" not in index.eligible_fqns_lower


def test_lineage_only_edge_with_missing_target_endpoint_is_unusable_not_a_crash():
    from core.sqlservice.join_planner import _build_join_graph_index
    view = _Doc("dbo.View1", object_type="VIEW", column_names=("Id",),
                confirmed_relationship_targets=("dbo.Base.*",),
                confirmed_relationship_edges=(_out("*", "dbo.Base.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    # "dbo.Base" (the edge's other endpoint) is deliberately never included.
    index = _build_join_graph_index([view])  # must not raise KeyError
    assert index.joinable_edges == ()
    assert index.lineage_edges == ()
    assert index.unusable_edge_count == 1
    assert "dbo.base" not in index.eligible_fqns_lower


def test_lineage_only_edge_with_both_endpoints_present_is_unaffected():
    """Behavior for the ordinary (both-endpoints-present) case must be
    byte-identical to before this fix — this is the same fixture as
    test_star_view_dependency_is_lineage_only_never_joinable, re-asserted
    here to guard the fix from changing it."""
    from core.sqlservice.join_planner import _build_join_graph_index
    base = _Doc("dbo.Base", column_names=("Id",), confirmed_relationship_sources=("dbo.View1.*",),
                confirmed_relationship_edges=(_in("*", "dbo.View1.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    view = _Doc("dbo.View1", object_type="VIEW", column_names=("Id",),
                confirmed_relationship_targets=("dbo.Base.*",),
                confirmed_relationship_edges=(_out("*", "dbo.Base.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    index = _build_join_graph_index([base, view])
    assert index.joinable_edges == ()
    assert len(index.lineage_edges) == 1
    assert index.lineage_edges[0].join_capability == JOIN_CAPABILITY_LINEAGE_ONLY
    assert index.unusable_edge_count == 0


def test_physical_fk_to_an_excluded_tier_endpoint_is_unusable_unchanged():
    """A non-lineage (PHYSICAL_FK) edge to a missing endpoint already went
    through the UNUSABLE branch before this fix — must remain unchanged."""
    from core.sqlservice.join_planner import _build_join_graph_index
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.Excluded.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.Excluded.Id"),))
    index = _build_join_graph_index([a])  # "dbo.Excluded" never included
    assert index.joinable_edges == ()
    assert index.lineage_edges == ()
    assert index.unusable_edge_count == 1


def test_catalog_view_dependency_with_both_endpoints_present_is_unchanged():
    """A second, independent both-present CATALOG_VIEW_DEPENDENCY fixture
    (composite-column shape) — behavior must be identical pre/post fix."""
    from core.sqlservice.join_planner import _build_join_graph_index
    base = _Doc(
        "dbo.Base", column_names=("Id", "Other"), confirmed_relationship_sources=("dbo.View2.*",),
        confirmed_relationship_edges=(_in("*", "dbo.View2.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),),
    )
    view = _Doc(
        "dbo.View2", object_type="VIEW", column_names=("Id",),
        confirmed_relationship_targets=("dbo.Base.*",),
        confirmed_relationship_edges=(_out("*", "dbo.Base.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),),
    )
    index = _build_join_graph_index([base, view])
    assert len(index.lineage_edges) == 1
    assert index.lineage_edges[0].join_capability == JOIN_CAPABILITY_LINEAGE_ONLY


def test_graph_index_build_is_deterministic_with_a_missing_lineage_endpoint():
    from core.sqlservice.join_planner import _build_join_graph_index
    base = _Doc("dbo.Base", column_names=("Id",), confirmed_relationship_sources=("dbo.View1.*",),
                confirmed_relationship_edges=(_in("*", "dbo.View1.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),))
    first = _build_join_graph_index([base])
    second = _build_join_graph_index([base])
    assert first.joinable_edges == second.joinable_edges
    assert first.lineage_edges == second.lineage_edges
    assert first.unusable_edge_count == second.unusable_edge_count
    assert first.eligible_fqns_lower == second.eligible_fqns_lower


def test_excluded_endpoint_never_appears_in_a_real_plan_via_plan_schema_join_subgraph():
    """End-to-end through plan_schema_join_subgraph (not just the index
    builder): an object reachable only via a lineage-only edge to an
    excluded Tier 3/4 object must still produce a normal, non-crashing
    single-object plan, and the excluded object must never appear
    anywhere in the result."""
    clear_join_planner_cache()
    doc = _Doc(
        "dbo.SurveyAnswers", column_names=("Id",),
        confirmed_relationship_sources=("dbo.SomeView.*",),
        confirmed_relationship_edges=(_in("*", "dbo.SomeView.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),),
    )
    # "dbo.SomeView" (Tier 3/4, excluded) is deliberately never included —
    # reproducing exactly what core.sqlservice.schema_plan_options passes
    # (tiers=(1, 2) only) when a Tier 1/2 object's only confirmed
    # relationship points at an excluded Tier 3/4 object.
    reqs = [_requirement("ENTITY:answer", [("dbo.SurveyAnswers", 0.9)])]
    result = _plan(reqs, [doc], anchor_fqns=("dbo.SurveyAnswers",))
    assert result.status == STATUS_OK
    plan = result.primary_plan
    assert plan.selected_object_fqns == ("dbo.SurveyAnswers",)
    assert "dbo.someview" not in {f.lower() for f in plan.selected_object_fqns}
    assert all("someview" not in edge.from_object_fqn.lower() and "someview" not in edge.to_object_fqn.lower()
               for edge in plan.join_edges)


# ── 19. Disconnected requirements ──────────────────────────────────────────


def test_disconnected_requirements_reported_not_fabricated():
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))  # no relationship to A at all
    docs = [a, b]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_PARTIAL_COVERAGE
    assert len(result.primary_plan.uncovered_requirement_keys) == 1
    assert len(result.primary_plan.covered_requirement_keys) == 1
    # Never both objects fabricated into one plan without a real edge.
    assert result.primary_plan.join_edges == ()


def test_no_candidates_at_all_returns_no_candidates_status():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:ghost", [("dbo.DoesNotExist", 0.9)])]
    result = _plan(reqs, docs)
    assert result.status == STATUS_NO_CANDIDATES
    assert result.primary_plan is None
    assert result.error is not None


def test_no_requirements_returns_no_requirements_status():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    result = _plan([], docs)
    assert result.status == STATUS_NO_REQUIREMENTS
    assert result.primary_plan is None


def test_upstream_linking_not_ok_short_circuits():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    link = _linking_result([_requirement("ENTITY:a", [("dbo.A", 0.9)])])
    import dataclasses
    not_ok = dataclasses.replace(link, status="NO_CONFIDENT_ANCHOR")
    result = plan_schema_join_subgraph(not_ok, docs)
    assert result.status == STATUS_NO_REQUIREMENTS
    assert result.primary_plan is None


# ── 20. Ambiguity between similar complete plans ───────────────────────────


def test_ambiguity_recorded_for_two_similarly_confident_terminal_choices():
    # Requirement "b" has two nearly-tied candidates, both directly
    # connected to the requirement "a" object.
    a = _Doc("dbo.A", column_names=("Id", "B1ID", "B2ID"),
             confirmed_relationship_targets=("dbo.B1.Id", "dbo.B2.Id"),
             confirmed_relationship_edges=(_out("B1ID", "dbo.B1.Id"), _out("B2ID", "dbo.B2.Id")))
    b1 = _Doc("dbo.B1", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B1ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B1ID"),))
    b2 = _Doc("dbo.B2", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B2ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B2ID"),))
    docs = [a, b1, b2]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [("dbo.B1", 0.8), ("dbo.B2", 0.78)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert len(result.ambiguity_records) >= 1
    record = result.ambiguity_records[0]
    assert record.concept_key == "ENTITY:b"
    assert {record.primary_object_fqn, record.alternative_object_fqn} == {"dbo.B1", "dbo.B2"}


# ── 21. Hub penalty ─────────────────────────────────────────────────────────


def test_hub_penalty_scores_lower_degree_bridge_higher():
    from core.sqlservice.join_planner import _PlanState, _Requirement, _score_state, _JoinGraphIndex
    from types import MappingProxyType as _MPT

    def _index_with_degrees(degree_by_fqn: dict[str, int]) -> "_JoinGraphIndex":
        return _JoinGraphIndex(
            fqn_by_lower=_MPT({k: k for k in degree_by_fqn}),
            eligible_fqns_lower=frozenset(degree_by_fqn),
            object_type_by_fqn_lower=_MPT({k: "TABLE" for k in degree_by_fqn}),
            joinable_adjacency=_MPT({k: () for k in degree_by_fqn}),
            joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
            degree_by_fqn_lower=_MPT(degree_by_fqn),
            max_degree=max(degree_by_fqn.values()),
            canonical_score_by_fqn_lower=_MPT({k: 0.5 for k in degree_by_fqn}),
        )

    requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("a", 0.8),),
        candidate_total_count=1, truncated_by_ceiling=False,
    )]
    edge = PlannedJoinEdge(
        from_object_fqn="a", to_object_fqn="bridge", column_pairs=(("id", "id"),),
        relationship_type=RELATIONSHIP_TYPE_PHYSICAL_FK, inference_method=RELATIONSHIP_TYPE_PHYSICAL_FK,
        direction=DIRECTION_FORWARD, join_capability=JOIN_CAPABILITY_JOINABLE,
    )
    state_low_hub = _PlanState(
        selected_lower=frozenset({"a", "bridge"}), selected_order=("a", "bridge"), edges=(edge,),
        terminal_choice_by_concept={"ENTITY:a": "a"},
    )
    index_low = _index_with_degrees({"a": 1, "bridge": 1, "other": 50})
    index_high = _index_with_degrees({"a": 1, "bridge": 50, "other": 50})

    key_low, score_low, _c1, _cov1 = _score_state(state_low_hub, requirements, index_low, 12)
    key_high, score_high, _c2, _cov2 = _score_state(state_low_hub, requirements, index_high, 12)
    assert key_low < key_high  # lower sort_key == ranked better (ascending sort)
    assert score_low > score_high


# ── Limits (10) ──────────────────────────────────────────────────────────


def test_max_terminal_candidates_per_requirement_truncates_and_is_reported():
    docs = [_Doc(f"dbo.Cand{i}", column_names=("Id",)) for i in range(10)]
    reqs = [_requirement("ENTITY:a", [(f"dbo.Cand{i}", 0.9 - i * 0.01) for i in range(10)])]
    limits = JoinPlanningLimits(max_terminal_candidates_per_requirement=3)
    result = _plan(reqs, docs, limits=limits)
    assert LIMIT_MAX_TERMINAL_CANDIDATES in result.limits_reached
    assert result.considered_candidate_count == 3


def test_max_selected_objects_limit_blocks_an_overlong_chain():
    docs = []
    prev = None
    for i in range(6):
        name = f"dbo.Obj{i}"
        meta = {"column_names": ("Id", "NextID")}
        if prev is not None:
            meta["confirmed_relationship_targets"] = (f"{prev}.Id",)
            meta["confirmed_relationship_edges"] = (_out("NextID", f"{prev}.Id"),)
        docs.append(_Doc(name, **meta))
        prev = name
    reqs = [_requirement("ENTITY:first", [("dbo.Obj0", 0.9)]), _requirement("ENTITY:last", [("dbo.Obj5", 0.9)])]
    limits = JoinPlanningLimits(max_selected_objects=3, max_path_hops=6)
    result = _plan(reqs, docs, limits=limits)
    assert LIMIT_MAX_SELECTED_OBJECTS in result.limits_reached or result.status == STATUS_PARTIAL_COVERAGE
    if result.primary_plan is not None:
        assert len(result.primary_plan.selected_object_fqns) <= 3


def test_max_path_hops_limit_is_reported_when_it_actually_truncates():
    docs = []
    prev = None
    for i in range(5):
        name = f"dbo.Chain{i}"
        meta = {"column_names": ("Id", "NextID")}
        if prev is not None:
            meta["confirmed_relationship_targets"] = (f"{prev}.Id",)
            meta["confirmed_relationship_edges"] = (_out("NextID", f"{prev}.Id"),)
        docs.append(_Doc(name, **meta))
        prev = name
    reqs = [_requirement("ENTITY:first", [("dbo.Chain0", 0.9)]), _requirement("ENTITY:last", [("dbo.Chain4", 0.9)])]
    limits = JoinPlanningLimits(max_path_hops=2, max_selected_objects=12)
    result = _plan(reqs, docs, limits=limits)
    assert LIMIT_MAX_PATH_HOPS in result.limits_reached
    assert result.status == STATUS_PARTIAL_COVERAGE


def test_operation_time_budget_of_zero_yields_limits_exceeded_or_empty_beam():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    limits = JoinPlanningLimits(operation_time_budget_ms=-1.0)
    result = _plan(reqs, docs, limits=limits)
    assert result.status in (STATUS_LIMITS_EXCEEDED, STATUS_OK, STATUS_PARTIAL_COVERAGE)
    # Whatever the outcome, no exception is raised and the budget is honored deterministically.


def test_max_alternative_plans_limit_is_reported():
    a = _Doc("dbo.A", column_names=("Id", "B1", "B2", "B3", "B4", "B5", "B6"),
             confirmed_relationship_targets=tuple(f"dbo.B{i}.Id" for i in range(1, 7)),
             confirmed_relationship_edges=tuple(_out(f"B{i}", f"dbo.B{i}.Id") for i in range(1, 7)))
    bs = [
        _Doc(f"dbo.B{i}", column_names=("Id",), confirmed_relationship_sources=(f"dbo.A.B{i}",),
             confirmed_relationship_edges=(_in("Id", f"dbo.A.B{i}"),))
        for i in range(1, 7)
    ]
    docs = [a] + bs
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [(f"dbo.B{i}", 0.5) for i in range(1, 7)]),
    ]
    limits = JoinPlanningLimits(max_alternative_plans=2)
    result = _plan(reqs, docs, limits=limits)
    assert len(result.alternative_plans) <= 2
    assert LIMIT_MAX_ALTERNATIVE_PLANS in result.limits_reached


# ── Deep immutability ───────────────────────────────────────────────────────


def test_result_and_nested_dataclasses_are_immutable():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    result = _plan(reqs, docs)
    with pytest.raises(Exception):
        result.status = "X"  # type: ignore[misc]
    with pytest.raises(Exception):
        result.primary_plan.score = 0.0  # type: ignore[misc]
    assert isinstance(result.primary_plan.selected_object_fqns, tuple)
    assert isinstance(result.primary_plan.join_edges, tuple)


# ── Shuffled input determinism ──────────────────────────────────────────────


def test_shuffled_document_and_candidate_order_produces_identical_plan():
    customers, orders, products = _bridge_schema()
    docs = [customers, orders, products]
    reqs = [_requirement("ENTITY:customer", [("dbo.Customers", 0.9)]), _requirement("ENTITY:product", [("dbo.Products", 0.9)])]
    baseline = _plan(reqs, docs)

    shuffled_docs = list(docs)
    random.Random(7).shuffle(shuffled_docs)
    clear_join_planner_cache()
    shuffled_result = _plan(reqs, shuffled_docs)

    assert shuffled_result.primary_plan.selected_object_fqns == baseline.primary_plan.selected_object_fqns
    assert shuffled_result.primary_plan.join_edges == baseline.primary_plan.join_edges
    assert shuffled_result.primary_plan.score == baseline.primary_plan.score


# ── Cache identity / clear / eviction ──────────────────────────────────────


def test_cache_reused_for_identical_documents():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    _plan(reqs, docs)
    _plan(reqs, docs)
    assert join_planner_cache_size() == 1


def test_clear_cache_evicts_everything():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    _plan(reqs, docs)
    assert join_planner_cache_size() > 0
    clear_join_planner_cache()
    assert join_planner_cache_size() == 0


def test_cache_eviction_is_bounded():
    from core.config import SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES
    for i in range(SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES + 5):
        docs = [_Doc(f"dbo.Object{i}", column_names=("Id",))]
        reqs = [_requirement("ENTITY:a", [(f"dbo.Object{i}", 0.9)])]
        _plan(reqs, docs)
    assert join_planner_cache_size() <= SQL_JOIN_PLANNING_MAX_CACHED_GRAPH_INDEXES


def test_cache_never_stores_a_plan_or_question():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    _plan(reqs, docs)
    for index in join_planner_module._graph_index_cache.values():
        for field_name in vars(index):
            assert "plan" not in field_name.lower() and "question" not in field_name.lower()


# ── Performance sanity ───────────────────────────────────────────────────


def test_warm_planning_is_fast_for_a_moderately_sized_schema():
    docs = []
    prev = None
    for i in range(300):
        name = f"dbo.Object{i}"
        meta = {"column_names": ("Id", "PrevID")}
        if prev is not None:
            meta["confirmed_relationship_targets"] = (f"{prev}.Id",)
            meta["confirmed_relationship_edges"] = (_out("PrevID", f"{prev}.Id"),)
        docs.append(_Doc(name, **meta))
        prev = name
    reqs = [_requirement("ENTITY:first", [("dbo.Object0", 0.9)]), _requirement("ENTITY:last", [("dbo.Object299", 0.9)])]
    link = _linking_result(reqs)
    plan_schema_join_subgraph(link, docs)  # build & cache the index once

    start = time.perf_counter()
    for _ in range(5):
        plan_schema_join_subgraph(link, docs)
    elapsed_ms = (time.perf_counter() - start) / 5 * 1000.0
    assert elapsed_ms < 500  # generous sanity bound, not a strict benchmark assertion


# ── No I/O, no the reference database/domain-specific tokens ─────────────────────────────────


_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "join_planner.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"join_planner.py contains banned domain-specific token {banned!r}"


def test_module_never_imports_database_network_or_provider_modules():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "join_planner.py").read_text(encoding="utf-8")
    for forbidden_import in ("data.db", "sqlite3", "requests", "openai", "urllib", "socket", "pyodbc", "embed_batch", "threading"):
        assert forbidden_import not in source, f"unexpected I/O-shaped reference {forbidden_import!r}"


# ── End-to-end integration (real linker + real catalog) ────────────────────


def test_end_to_end_with_real_schema_linker_and_catalog():
    customers = _Doc(
        "dbo.Customers", business_name="Customer",
        column_names=("CustomerID", "Name"), has_primary_key=True, primary_key_columns=("CustomerID",),
        confirmed_relationship_sources=("dbo.Orders.CustomerID",),
        confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),),
    )
    orders = _Doc(
        "dbo.Orders", business_name="Order",
        column_names=("OrderID", "CustomerID", "Amount"), has_primary_key=True, primary_key_columns=("OrderID",),
        measure_columns=("Amount",),
        confirmed_relationship_targets=("dbo.Customers.CustomerID",),
        confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),),
        column_descriptors=(
            {"column_name": "OrderID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": True, "is_foreign_key": False},
            {"column_name": "CustomerID", "normalized_sql_type": "INTEGER", "is_nullable": False, "is_primary_key": False, "is_foreign_key": True},
            {"column_name": "Amount", "normalized_sql_type": "DECIMAL", "is_nullable": True, "is_primary_key": False, "is_foreign_key": False},
        ),
    )
    docs = [customers, orders]
    catalog = build_catalog(docs)
    link = link_question_to_schema("What is the total order amount for each customer?", catalog, docs)
    assert link.status == "OK"

    result = plan_schema_join_subgraph(link, docs)
    assert result.status == STATUS_OK
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders"}
    assert len(result.primary_plan.join_edges) == 1


def test_no_expected_objects_parameter_exists():
    import inspect
    sig = inspect.signature(plan_schema_join_subgraph)
    for name in sig.parameters:
        assert "expected" not in name.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Step 3B — requirement roles, single-object preservation, lazy search
# ═══════════════════════════════════════════════════════════════════════════

from core.sqlservice.join_planner import (  # noqa: E402
    PLAN_KIND_AMBIGUOUS,
    PLAN_KIND_JOINED_SUBGRAPH,
    PLAN_KIND_PARTIAL,
    PLAN_KIND_SINGLE_OBJECT,
    REASON_LINEAGE_ONLY_PATH,
    REASON_NO_CONFIRMED_JOIN_PATH,
    REASON_NO_JOIN_REQUIRED,
    REASON_SEARCH_BUDGET_EXHAUSTED,
    REQUIREMENT_ROLE_BUSINESS_REQUIRED,
    REQUIREMENT_ROLE_FILTER_OR_GROUPING,
    REQUIREMENT_ROLE_JOIN_HINT,
    REQUIREMENT_ROLE_UNRESOLVED,
)


def _messy_noise_schema(n_noise: int = 20) -> list:
    """A schema shaped like the real Step 3A finding: one obviously-correct
    entity (dbo.Student, isolated — no confirmed relationship at all) plus
    many single-candidate "noise" entities that also happen to be mutually
    disconnected. Regression fixture for "correct single-table linker
    result preserved"."""
    docs = [_Doc("dbo.Student", column_names=("Id",))]
    for i in range(n_noise):
        docs.append(_Doc(f"dbo.Noise{i}", column_names=("Id",)))
    return docs


def test_correct_single_table_linker_result_preserved_against_noise():
    """The question's single, high-confidence entity concept must win as
    the primary plan even when many other, lower-confidence single-
    candidate entity concepts were also matched (none of them connected to
    anything) — this is the exact REF2-001 regression this Step fixes."""
    docs = _messy_noise_schema(n_noise=15)
    reqs = [_requirement("ENTITY:student", [("dbo.Student", 0.99)])]
    reqs += [_requirement(f"ENTITY:noise{i}", [(f"dbo.Noise{i}", 0.6)]) for i in range(15)]
    result = _plan(reqs, docs, anchor_fqns=("dbo.Student", *(f"dbo.Noise{i}" for i in range(15))))
    assert result.primary_plan is not None
    # dbo.Student must be SELECTED as primary — it is genuinely the best
    # connected answer available. plan_kind is honestly PARTIAL (not a
    # false SINGLE_OBJECT-complete claim) because the 15 other isolated
    # noise concepts are real BUSINESS_REQUIRED concepts nothing can cover.
    assert result.primary_plan.selected_object_fqns == ("dbo.Student",)
    assert "ENTITY:student" in result.primary_plan.covered_requirement_keys


def test_lineage_only_object_succeeds_as_single_object_plan():
    """An object whose ONLY confirmed relationship is lineage-only must
    still be a COMPLETE, non-partial single-object plan when no join is
    actually required — requirement 7."""
    doc = _Doc(
        "dbo.SurveyAnswers", column_names=("Id",),
        confirmed_relationship_sources=("dbo.SomeView.*",),
        confirmed_relationship_edges=(_in("*", "dbo.SomeView.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),),
    )
    view = _Doc("dbo.SomeView", object_type="VIEW", column_names=("Id",))
    reqs = [_requirement("ENTITY:answer", [("dbo.SurveyAnswers", 0.9)])]
    result = _plan(reqs, [doc, view], anchor_fqns=("dbo.SurveyAnswers",))
    assert result.status == STATUS_OK
    plan = result.primary_plan
    assert plan.selected_object_fqns == ("dbo.SurveyAnswers",)
    assert plan.plan_kind == PLAN_KIND_SINGLE_OBJECT
    assert plan.uncovered_requirement_keys == ()
    assert any(REASON_NO_JOIN_REQUIRED in w for w in plan.warnings)


def test_joined_plan_cannot_beat_complete_single_object_without_added_business_coverage():
    """A connected multi-object plan that covers the SAME single business
    concept (never anything more) must never outrank the smaller complete
    single-object plan — requirement 5's "a complete single-object plan
    must beat a joined plan that adds no business coverage"."""
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.Bridge.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.Bridge.Id"),))
    bridge = _Doc("dbo.Bridge", column_names=("Id",), confirmed_relationship_sources=("dbo.A.BID",),
                   confirmed_relationship_edges=(_in("Id", "dbo.A.BID"),))
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    result = _plan(reqs, [a, bridge], anchor_fqns=("dbo.A",))
    assert result.primary_plan.selected_object_fqns == ("dbo.A",)
    assert result.primary_plan.plan_kind == PLAN_KIND_SINGLE_OBJECT


def test_identifier_requirement_is_join_hint_not_mandatory_coverage():
    """A widely-repeated identifier concept must never appear in covered/
    uncovered requirement lists, and must never by itself demand a second
    object be added to an otherwise-complete single-object plan."""
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.95)]),
        _requirement("IDENTIFIER:shared", [("dbo.A", 0.3), ("dbo.B", 0.3)]),
    ]
    result = _plan(reqs, [a, b], anchor_fqns=("dbo.A", "dbo.B"))
    plan = result.primary_plan
    assert plan.selected_object_fqns == ("dbo.A",)
    assert "IDENTIFIER:shared" not in plan.covered_requirement_keys
    assert "IDENTIFIER:shared" not in plan.uncovered_requirement_keys
    assert result.status == STATUS_OK


def test_candidate_ranked_beyond_former_cutoff_is_discovered():
    """A candidate ranked 9th (or later) among ~15-20 total must still be
    reachable and connectable — the former Step 3 max_terminal_candidates_
    per_requirement=5 default would have excluded it outright."""
    b_docs = []
    for i in range(20):
        b_docs.append(_Doc(f"dbo.B{i}", column_names=("Id",)))
    a = _Doc(
        "dbo.A", column_names=("Id", "B12ID"),
        confirmed_relationship_targets=("dbo.B12.Id",),
        confirmed_relationship_edges=(_out("B12ID", "dbo.B12.Id"),),
    )
    docs = [a, *b_docs]
    # Rank the CONNECTED candidate (B12) 13th by confidence — well beyond
    # the old default cutoff of 5.
    candidates = [(f"dbo.B{i}", 0.9 - i * 0.01) for i in range(20)]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.95)]),
        _requirement("ENTITY:b", candidates),
    ]
    result = _plan(reqs, docs, anchor_fqns=("dbo.A", "dbo.B12"))
    assert result.status == STATUS_OK
    assert "dbo.B12" in result.primary_plan.selected_object_fqns
    assert result.considered_candidate_count >= 13


def test_lazy_all_candidate_search_considers_full_candidate_list_by_default():
    docs = [_Doc(f"dbo.Cand{i}", column_names=("Id",)) for i in range(30)]
    reqs = [_requirement("ENTITY:a", [(f"dbo.Cand{i}", 0.9 - i * 0.01) for i in range(30)])]
    result = _plan(reqs, docs)
    assert result.considered_candidate_count == 30
    assert LIMIT_MAX_TERMINAL_CANDIDATES not in result.limits_reached


def test_global_operation_budget_stops_safely_without_crashing():
    docs = []
    prev = None
    for i in range(40):
        name = f"dbo.Chain{i}"
        meta = {"column_names": ("Id", "NextID")}
        if prev is not None:
            meta["confirmed_relationship_targets"] = (f"{prev}.Id",)
            meta["confirmed_relationship_edges"] = (_out("NextID", f"{prev}.Id"),)
        docs.append(_Doc(name, **meta))
        prev = name
    reqs = [_requirement("ENTITY:first", [("dbo.Chain0", 0.9)]), _requirement("ENTITY:last", [("dbo.Chain39", 0.9)])]
    limits = JoinPlanningLimits(operation_time_budget_ms=0.0, max_path_hops=6, max_selected_objects=40)
    result = _plan(reqs, docs, limits=limits)
    # Must never raise; must produce SOME deterministic, well-typed result.
    assert result.status in (STATUS_OK, STATUS_PARTIAL_COVERAGE, STATUS_LIMITS_EXCEEDED)


def test_budget_exhaustion_distinguished_from_no_relationship():
    """A candidate that genuinely has no confirmed relationship must be
    classified REASON_NO_CONFIRMED_JOIN_PATH, never REASON_SEARCH_BUDGET_
    EXHAUSTED, even when OTHER unrelated limits fired during the same call."""
    isolated = _Doc("dbo.Isolated", column_names=("Id",))
    docs = [isolated] + [_Doc(f"dbo.Filler{i}", column_names=("Id",)) for i in range(10)]
    reqs = [_requirement("ENTITY:isolated", [("dbo.Isolated", 0.9)])]
    reqs += [_requirement(f"ENTITY:filler{i}", [(f"dbo.Filler{i}", 0.5)]) for i in range(10)]
    result = _plan(reqs, docs)
    # dbo.Isolated should win (highest confidence, single concept) —
    # nothing left uncovered that could be confused with a budget issue.
    assert result.primary_plan.selected_object_fqns == ("dbo.Isolated",)
    reasons = {r.reason for r in result.primary_plan.uncovered_requirement_reasons}
    assert REASON_SEARCH_BUDGET_EXHAUSTED not in reasons


def test_complete_high_confidence_linker_solution_cannot_regress():
    """Requirement 6's linker preservation contract, phrased directly: when
    a single anchor alone covers 100% of BUSINESS_REQUIRED concepts, no
    combination of lower-value filter/grouping noise on OTHER objects may
    ever displace it."""
    correct = _Doc("dbo.Correct", column_names=("Id",))
    distractor = _Doc("dbo.Distractor", column_names=("Id",))
    reqs = [
        _requirement("ENTITY:thing", [("dbo.Correct", 0.95)]),
        _requirement("DIMENSION:noise1", [("dbo.Distractor", 0.9)]),
        _requirement("DATE_ROLE:noise2", [("dbo.Distractor", 0.9)]),
    ]
    result = _plan(reqs, [correct, distractor], anchor_fqns=("dbo.Correct", "dbo.Distractor"))
    assert result.primary_plan.selected_object_fqns == ("dbo.Correct",)
    assert result.status == STATUS_OK


def test_multi_entity_question_still_chooses_joined_subgraph():
    customers, orders, products = None, None, None
    customers = _Doc("dbo.Customers", column_names=("CustomerID",),
                      confirmed_relationship_sources=("dbo.Orders.CustomerID",),
                      confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),))
    orders = _Doc("dbo.Orders", column_names=("OrderID", "CustomerID"),
                   confirmed_relationship_targets=("dbo.Customers.CustomerID",),
                   confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),))
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
    ]
    result = _plan(reqs, [customers, orders], anchor_fqns=("dbo.Customers", "dbo.Orders"))
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders"}
    assert result.primary_plan.plan_kind == PLAN_KIND_JOINED_SUBGRAPH
    assert result.status == STATUS_OK


def test_bridge_table_recovery_with_zero_question_relevance():
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.Bridge.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.Bridge.Id"),))
    bridge = _Doc("dbo.Bridge", column_names=("Id", "CID"),
                   confirmed_relationship_sources=("dbo.A.BID",), confirmed_relationship_targets=("dbo.C.Id",),
                   confirmed_relationship_edges=(_in("Id", "dbo.A.BID"), _out("CID", "dbo.C.Id")))
    c = _Doc("dbo.C", column_names=("Id",), confirmed_relationship_sources=("dbo.Bridge.CID",),
             confirmed_relationship_edges=(_in("Id", "dbo.Bridge.CID"),))
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:c", [("dbo.C", 0.9)])]
    result = _plan(reqs, [a, bridge, c], anchor_fqns=("dbo.A", "dbo.C"))
    assert result.status == STATUS_OK
    assert result.primary_plan.bridge_object_fqns == ("dbo.Bridge",)
    assert result.primary_plan.plan_kind == PLAN_KIND_JOINED_SUBGRAPH


def test_disconnected_business_entities_return_partial():
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))  # no relationship to A
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    result = _plan(reqs, [a, b], anchor_fqns=("dbo.A", "dbo.B"))
    assert result.status == STATUS_PARTIAL_COVERAGE
    assert result.primary_plan.plan_kind == PLAN_KIND_PARTIAL
    assert len(result.primary_plan.uncovered_requirement_keys) == 1
    reasons = {r.reason for r in result.primary_plan.uncovered_requirement_reasons}
    assert REASON_NO_CONFIRMED_JOIN_PATH in reasons


def test_ambiguous_plan_kind_set_when_genuine_ambiguity_detected():
    a = _Doc("dbo.A", column_names=("Id", "B1ID", "B2ID"),
             confirmed_relationship_targets=("dbo.B1.Id", "dbo.B2.Id"),
             confirmed_relationship_edges=(_out("B1ID", "dbo.B1.Id"), _out("B2ID", "dbo.B2.Id")))
    b1 = _Doc("dbo.B1", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B1ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B1ID"),))
    b2 = _Doc("dbo.B2", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B2ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B2ID"),))
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [("dbo.B1", 0.8), ("dbo.B2", 0.78)]),
    ]
    result = _plan(reqs, [a, b1, b2], anchor_fqns=("dbo.A", "dbo.B1", "dbo.B2"))
    assert result.status == STATUS_OK
    assert result.primary_plan.plan_kind == PLAN_KIND_AMBIGUOUS
    assert len(result.ambiguity_records) >= 1


def test_requirement_roles_classified_by_concept_kind():
    from core.sqlservice.join_planner import _extract_requirements, _build_join_graph_index, DEFAULT_JOIN_PLANNING_LIMITS
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("MEASURE:m", [("dbo.A", 0.9)]),
        _requirement("DIMENSION:d", [("dbo.A", 0.9)]),
        _requirement("DATE_ROLE:r", [("dbo.A", 0.9)]),
        _requirement("IDENTIFIER:i", [("dbo.A", 0.9)]),
    ]
    link = _linking_result(reqs)
    index = _build_join_graph_index(docs)
    extracted, _count = _extract_requirements(link, index, DEFAULT_JOIN_PLANNING_LIMITS, [], [])
    role_by_key = {r.concept_key: r.role for r in extracted}
    assert role_by_key["ENTITY:a"] == REQUIREMENT_ROLE_BUSINESS_REQUIRED
    assert role_by_key["MEASURE:m"] == REQUIREMENT_ROLE_BUSINESS_REQUIRED
    assert role_by_key["DIMENSION:d"] == REQUIREMENT_ROLE_FILTER_OR_GROUPING
    assert role_by_key["DATE_ROLE:r"] == REQUIREMENT_ROLE_FILTER_OR_GROUPING
    assert role_by_key["IDENTIFIER:i"] == REQUIREMENT_ROLE_JOIN_HINT


def test_role_mapping_driven_by_strength_not_concept_kind():
    """Step 3C, requirement 5: role is a function of `strength`, never of
    `concept_kind` alone. A DIMENSION explicitly promoted to CORE by the
    linker (e.g. a grouped category) must count as BUSINESS_REQUIRED; an
    ENTITY the linker only weakly supported (SUPPORTING) must NOT count as
    BUSINESS_REQUIRED even though entities defaulted to CORE historically."""
    from core.sqlservice.join_planner import _extract_requirements, _build_join_graph_index, DEFAULT_JOIN_PLANNING_LIMITS
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [
        _requirement("ENTITY:weak", [("dbo.A", 0.4)], strength="SUPPORTING"),
        _requirement("DIMENSION:strong", [("dbo.A", 0.9)], strength="CORE"),
        _requirement("MEASURE:unresolved", [("dbo.A", 0.5)], strength="UNRESOLVED"),
        _requirement("DATE_ROLE:noted", [("dbo.A", 0.5)], strength="CONSTRAINT"),
    ]
    link = _linking_result(reqs)
    index = _build_join_graph_index(docs)
    extracted, _count = _extract_requirements(link, index, DEFAULT_JOIN_PLANNING_LIMITS, [], [])
    role_by_key = {r.concept_key: r.role for r in extracted}
    assert role_by_key["ENTITY:weak"] == REQUIREMENT_ROLE_JOIN_HINT
    assert role_by_key["DIMENSION:strong"] == REQUIREMENT_ROLE_BUSINESS_REQUIRED
    assert role_by_key["MEASURE:unresolved"] == REQUIREMENT_ROLE_UNRESOLVED
    assert role_by_key["DATE_ROLE:noted"] == REQUIREMENT_ROLE_FILTER_OR_GROUPING


def test_deterministic_primary_and_alternatives_across_repeated_calls():
    a = _Doc("dbo.A", column_names=("Id", "B1ID", "B2ID"),
             confirmed_relationship_targets=("dbo.B1.Id", "dbo.B2.Id"),
             confirmed_relationship_edges=(_out("B1ID", "dbo.B1.Id"), _out("B2ID", "dbo.B2.Id")))
    b1 = _Doc("dbo.B1", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B1ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B1ID"),))
    b2 = _Doc("dbo.B2", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B2ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B2ID"),))
    docs = [a, b1, b2]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [("dbo.B1", 0.8), ("dbo.B2", 0.78)]),
    ]
    results = [_plan(reqs, docs, anchor_fqns=("dbo.A", "dbo.B1", "dbo.B2")) for _ in range(5)]
    first = results[0]
    for r in results[1:]:
        assert r.primary_plan.selected_object_fqns == first.primary_plan.selected_object_fqns
        assert r.primary_plan.score == first.primary_plan.score
        assert [p.selected_object_fqns for p in r.alternative_plans] == [
            p.selected_object_fqns for p in first.alternative_plans
        ]


def test_no_reference_specific_behavior_step3b():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "join_planner.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source


# ═══════════════════════════════════════════════════════════════════════════
# Step 3E — cache-independent timing/typed failures/connection-time
# preparation, and terminal-priority scoring order
# ═══════════════════════════════════════════════════════════════════════════


def test_timing_fields_present_and_consistent():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    result = _plan(reqs, docs, anchor_fqns=("dbo.A",))
    assert result.index_load_or_build_ms >= 0.0
    assert result.join_search_ms >= 0.0
    assert abs(result.total_ms - (result.index_load_or_build_ms + result.join_search_ms)) < 1.0


def test_cold_and_warm_planner_output_is_structurally_identical():
    customers = _Doc("dbo.Customers", column_names=("CustomerID",),
                      confirmed_relationship_sources=("dbo.Orders.CustomerID",),
                      confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),))
    orders = _Doc("dbo.Orders", column_names=("OrderID", "CustomerID"),
                   confirmed_relationship_targets=("dbo.Customers.CustomerID",),
                   confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),))
    docs = [customers, orders]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
    ]
    clear_join_planner_cache()
    cold = _plan(reqs, docs, anchor_fqns=("dbo.Customers", "dbo.Orders"))
    warm = _plan(reqs, docs, anchor_fqns=("dbo.Customers", "dbo.Orders"))
    assert cold.primary_plan.selected_object_fqns == warm.primary_plan.selected_object_fqns
    assert cold.primary_plan.plan_kind == warm.primary_plan.plan_kind
    assert cold.primary_plan.join_edges == warm.primary_plan.join_edges
    assert cold.status == warm.status
    assert set(cold.limits_reached) == set(warm.limits_reached)


def test_cache_eviction_changes_latency_only():
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    before = _plan(reqs, docs, anchor_fqns=("dbo.A",))
    clear_join_planner_cache()
    after = _plan(reqs, docs, anchor_fqns=("dbo.A",))
    assert before.primary_plan.selected_object_fqns == after.primary_plan.selected_object_fqns
    assert before.status == after.status


def test_index_construction_excluded_from_search_budget():
    """A budget too small to do ANY beam-search work must still let index
    construction complete and the single-object baseline evaluate fairly
    -- requirement 3: index build never consumes the per-question budget."""
    docs = [_Doc("dbo.A", column_names=("Id",))]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)])]
    clear_join_planner_cache()
    limits = JoinPlanningLimits(operation_time_budget_ms=0.0)
    result = _plan(reqs, docs, anchor_fqns=("dbo.A",), limits=limits)
    assert result.status == STATUS_OK
    assert result.primary_plan.selected_object_fqns == ("dbo.A",)


def test_tiny_search_budget_still_begins_after_index_availability():
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.B.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.B.Id"),))
    b = _Doc("dbo.B", column_names=("Id",), confirmed_relationship_sources=("dbo.A.BID",),
             confirmed_relationship_edges=(_in("Id", "dbo.A.BID"),))
    docs = [a, b]
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.9)])]
    clear_join_planner_cache()
    limits = JoinPlanningLimits(operation_time_budget_ms=0.0)
    result = _plan(reqs, docs, anchor_fqns=("dbo.A", "dbo.B"), limits=limits)
    # Never crashes, never NOT_PREPARED/INDEX_BUILD_FAILED purely because
    # the search budget is tiny -- the index itself always finishes first.
    assert result.status in (STATUS_OK, STATUS_PARTIAL_COVERAGE, STATUS_LIMITS_EXCEEDED)
    assert result.index_load_or_build_ms >= 0.0


def test_index_build_failure_returns_typed_status_not_partial_plan():
    class _BrokenDoc:
        object_fqn = "dbo.Broken"
        object_type = "TABLE"
        retrieval_tier = 1

        @property
        def metadata(self):
            raise RuntimeError("simulated metadata access failure")

    docs = [_BrokenDoc()]
    reqs = [_requirement("ENTITY:a", [("dbo.Broken", 0.9)])]
    clear_join_planner_cache()
    link = _linking_result(reqs, ("dbo.Broken",))
    result = plan_schema_join_subgraph(link, docs)
    assert result.status == STATUS_INDEX_BUILD_FAILED
    assert result.primary_plan is None


def test_connection_time_preparation_builds_both_indexes_without_a_question():
    from core.sqlservice.schema_linker import schema_linker_cache_size
    docs = [_Doc("dbo.A", column_names=("Id",))]
    catalog = build_catalog(docs)
    clear_join_planner_cache()
    result = prepare_schema_planning_indexes(catalog, docs)
    assert result.status == STATUS_OK
    assert result.linker_index_ready is True
    assert result.join_graph_index_ready is True
    assert join_planner_cache_size() == 1
    assert schema_linker_cache_size() >= 1


def test_connection_time_preparation_typed_not_prepared_for_empty_documents():
    catalog = build_catalog([_Doc("dbo.A", column_names=("Id",))])
    result = prepare_schema_planning_indexes(catalog, [])
    assert result.status == STATUS_NOT_PREPARED
    assert result.linker_index_ready is False
    assert result.join_graph_index_ready is False


# ── Part 4: terminal-priority scoring order ────────────────────────────────


def test_exact_question_evidence_beats_graph_central_competitor():
    """Two single-object candidates cover the SAME (and only) requirement.
    The competitor is far more structurally connected/central (verified
    defect 4's "graph centrality can beat stronger direct evidence") but
    the correct object has far stronger direct question evidence (anchor
    combined_score) -- evidence must win at equal coverage."""
    correct = _Doc("dbo.Correct", column_names=("Id",))
    central = _Doc(
        "dbo.Central", column_names=("Id", "N1", "N2", "N3"),
        confirmed_relationship_targets=("dbo.N1.Id", "dbo.N2.Id", "dbo.N3.Id"),
        confirmed_relationship_edges=(
            _out("N1", "dbo.N1.Id"), _out("N2", "dbo.N2.Id"), _out("N3", "dbo.N3.Id"),
        ),
    )
    n1 = _Doc("dbo.N1", column_names=("Id",), confirmed_relationship_sources=("dbo.Central.N1",),
              confirmed_relationship_edges=(_in("Id", "dbo.Central.N1"),))
    n2 = _Doc("dbo.N2", column_names=("Id",), confirmed_relationship_sources=("dbo.Central.N2",),
              confirmed_relationship_edges=(_in("Id", "dbo.Central.N2"),))
    n3 = _Doc("dbo.N3", column_names=("Id",), confirmed_relationship_sources=("dbo.Central.N3",),
              confirmed_relationship_edges=(_in("Id", "dbo.Central.N3"),))
    docs = [correct, central, n1, n2, n3]
    reqs = [_requirement("ENTITY:thing", [("dbo.Correct", 0.6), ("dbo.Central", 0.6)])]
    result = _plan(
        reqs, docs, anchor_fqns=("dbo.Correct", "dbo.Central"),
        anchor_scores={"dbo.Correct": 0.95, "dbo.Central": 0.3},
    )
    assert result.primary_plan.selected_object_fqns == ("dbo.Correct",)


def test_join_validity_matters_when_multiple_entities_genuinely_require_a_join():
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.BGood.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.BGood.Id"),))
    b_good = _Doc("dbo.BGood", column_names=("Id",), confirmed_relationship_sources=("dbo.A.BID",),
                  confirmed_relationship_edges=(_in("Id", "dbo.A.BID"),))
    b_bad = _Doc(
        "dbo.BBad", column_names=("Id",), confirmed_relationship_sources=("dbo.A.*",),
        confirmed_relationship_edges=(_in("*", "dbo.A.*", kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY),),
    )
    docs = [a, b_good, b_bad]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [("dbo.BGood", 0.7), ("dbo.BBad", 0.7)]),
    ]
    result = _plan(reqs, docs, anchor_fqns=("dbo.A", "dbo.BGood", "dbo.BBad"))
    assert "dbo.BGood" in result.primary_plan.selected_object_fqns
    for edge in result.primary_plan.join_edges:
        assert edge.join_capability == JOIN_CAPABILITY_JOINABLE


def test_centrality_is_only_a_bounded_final_tie_break():
    """Extends the existing hub-penalty unit test (see
    test_hub_penalty_scores_lower_degree_bridge_higher): with every OTHER
    tier held perfectly identical (same coverage/relevance/joinability/
    size/canonical), a lower-degree bridge sorts strictly better than a
    higher-degree one -- but the moment an EARLIER tier (relevance) is
    given a real difference, that difference alone decides the outcome
    regardless of which state has the lower-degree bridge, proving
    centrality is subordinate to it, never able to override it."""
    from core.sqlservice.join_planner import _PlanState, _Requirement, _score_state, _JoinGraphIndex
    from types import MappingProxyType as _MPT

    def _index_with_degrees(degree_by_fqn: dict[str, int]) -> "_JoinGraphIndex":
        return _JoinGraphIndex(
            fqn_by_lower=_MPT({k: k for k in degree_by_fqn}),
            eligible_fqns_lower=frozenset(degree_by_fqn),
            object_type_by_fqn_lower=_MPT({k: "TABLE" for k in degree_by_fqn}),
            joinable_adjacency=_MPT({k: () for k in degree_by_fqn}),
            joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
            degree_by_fqn_lower=_MPT(degree_by_fqn),
            max_degree=max(degree_by_fqn.values()),
            canonical_score_by_fqn_lower=_MPT({k: 0.5 for k in degree_by_fqn}),
        )

    edge = PlannedJoinEdge(
        from_object_fqn="a", to_object_fqn="bridge", column_pairs=(("id", "id"),),
        relationship_type=RELATIONSHIP_TYPE_PHYSICAL_FK, inference_method=RELATIONSHIP_TYPE_PHYSICAL_FK,
        direction=DIRECTION_FORWARD, join_capability=JOIN_CAPABILITY_JOINABLE,
    )
    state = _PlanState(
        selected_lower=frozenset({"a", "bridge"}), selected_order=("a", "bridge"), edges=(edge,),
        terminal_choice_by_concept={"ENTITY:a": "a"},
    )
    index_quiet_bridge = _index_with_degrees({"a": 1, "bridge": 1, "other": 50})
    index_busy_bridge = _index_with_degrees({"a": 1, "bridge": 50, "other": 50})

    # Everything else tied: centrality alone correctly breaks the tie.
    tied_requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("a", 0.8),),
        candidate_total_count=1, truncated_by_ceiling=False,
    )]
    key_quiet, *_r1 = _score_state(state, tied_requirements, index_quiet_bridge, 12)
    key_busy, *_r2 = _score_state(state, tied_requirements, index_busy_bridge, 12)
    assert key_quiet < key_busy  # lower sort_key sorts first == wins

    # Now give the BUSY-bridge state materially stronger direct relevance
    # (a real evidence difference at an EARLIER tier) -- it must win
    # despite its worse (higher-degree) bridge, proving centrality can
    # never override a genuine evidence difference.
    strong_evidence_requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("a", 0.99),),
        candidate_total_count=1, truncated_by_ceiling=False,
    )]
    key_busy_strong_evidence, *_r3 = _score_state(state, strong_evidence_requirements, index_busy_bridge, 12)
    assert key_busy_strong_evidence < key_quiet


def test_complete_core_coverage_always_beats_incomplete_coverage():
    complete = _Doc("dbo.Complete", column_names=("Id",))
    partial = _Doc("dbo.Partial", column_names=("Id",))
    reqs = [
        _requirement("ENTITY:thing", [("dbo.Complete", 0.5)]),
        _requirement("ENTITY:other", [("dbo.Complete", 0.5)]),
    ]
    # dbo.Partial covers only ONE of the two requirements but has much
    # higher raw relevance for it -- complete coverage still wins.
    reqs_partial_competitor = [
        _requirement("ENTITY:thing", [("dbo.Complete", 0.5), ("dbo.Partial", 0.99)]),
        _requirement("ENTITY:other", [("dbo.Complete", 0.5)]),
    ]
    result = _plan(reqs_partial_competitor, [complete, partial], anchor_fqns=("dbo.Complete", "dbo.Partial"))
    assert set(result.primary_plan.uncovered_requirement_keys) == set()
    assert "dbo.Complete" in result.primary_plan.selected_object_fqns


# ═══════════════════════════════════════════════════════════════════════════
# Step 3F, Part 3/4/5 — coverage-state frontier robustness
# ═══════════════════════════════════════════════════════════════════════════


def _x_y_crowding_fixture(noise_count: int = 20):
    x_true = _Doc("dbo.XTrue", column_names=("Id", "YID"), confirmed_relationship_targets=("dbo.YTrue.Id",),
                  confirmed_relationship_edges=(_out("YID", "dbo.YTrue.Id"),))
    y_true = _Doc("dbo.YTrue", column_names=("Id",), confirmed_relationship_sources=("dbo.XTrue.YID",),
                  confirmed_relationship_edges=(_in("Id", "dbo.XTrue.YID"),))
    noise_docs = [_Doc(f"dbo.Noise{i}", column_names=("Id",)) for i in range(noise_count)]
    docs = [x_true, y_true, *noise_docs]
    x_candidates = [("dbo.XTrue", 0.5)] + [(f"dbo.Noise{i}", 0.5) for i in range(noise_count)]
    reqs = [
        _requirement("ENTITY:y", [("dbo.YTrue", 0.9)]),  # processed first (higher confidence)
        _requirement("ENTITY:x", x_candidates),
    ]
    anchor_fqns = ("dbo.XTrue", "dbo.YTrue", *[f"dbo.Noise{i}" for i in range(noise_count)])
    return reqs, docs, anchor_fqns


def test_multiple_candidates_for_one_requirement_cannot_crowd_out_another_requirement():
    """The exact Step 3E/3F regression: ENTITY:x has 21 candidates (as real
    vector evidence tends to produce for a popular concept). The single
    complete-coverage combination (XTrue+YTrue, joined) must still be found
    -- it lives in its OWN coverage-signature bucket ({ENTITY:x, ENTITY:y}),
    structurally immune to however many tied/noise candidates flood the
    {ENTITY:x}-only bucket."""
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    result = _plan(reqs, docs, anchor_fqns=anchor_fqns)
    assert set(result.primary_plan.uncovered_requirement_keys) == set()
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.XTrue", "dbo.YTrue"}


def test_candidate_reordering_does_not_remove_complete_coverage():
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    forward = _plan(reqs, docs, anchor_fqns=anchor_fqns)

    reversed_reqs = [
        reqs[0],
        _requirement("ENTITY:x", list(reversed(reqs[1].candidates and [(c.object_fqn, c.confidence) for c in reqs[1].candidates]))),
    ]
    reversed_result = _plan(reversed_reqs, docs, anchor_fqns=anchor_fqns)
    assert set(forward.primary_plan.selected_object_fqns) == set(reversed_result.primary_plan.selected_object_fqns)
    assert forward.primary_plan.plan_kind == reversed_result.primary_plan.plan_kind


def test_adding_vector_candidates_cannot_remove_existing_complete_plan():
    reqs_small, docs_small, anchors_small = _x_y_crowding_fixture(noise_count=2)
    baseline = _plan(reqs_small, docs_small, anchor_fqns=anchors_small)
    assert set(baseline.primary_plan.selected_object_fqns) == {"dbo.XTrue", "dbo.YTrue"}

    # Simulate added vector evidence: many MORE candidates for ENTITY:x.
    reqs_large, docs_large, anchors_large = _x_y_crowding_fixture(noise_count=20)
    grown = _plan(reqs_large, docs_large, anchor_fqns=anchors_large)
    assert set(grown.primary_plan.selected_object_fqns) == {"dbo.XTrue", "dbo.YTrue"}


def test_frontier_preserves_per_coverage_signature_diversity():
    from core.sqlservice.join_planner import (
        _build_join_graph_index, _extract_requirements, _run_beam_search,
        _covered_business_concepts, _covered_filter_concepts, DEFAULT_JOIN_PLANNING_LIMITS,
    )
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    link = _linking_result(reqs, anchor_fqns)
    index = _build_join_graph_index(docs)
    extracted, _count = _extract_requirements(link, index, DEFAULT_JOIN_PLANNING_LIMITS, [], [])
    beam, _considered, _limits, _expanded = _run_beam_search(
        extracted, index, DEFAULT_JOIN_PLANNING_LIMITS, time.perf_counter(),
    )
    signatures = {
        frozenset(_covered_business_concepts(s, extracted) | _covered_filter_concepts(s, extracted))
        for s in beam
    }
    assert frozenset({"ENTITY:x", "ENTITY:y"}) in signatures
    assert len(signatures) >= 2  # real diversity retained, not collapsed to one dominant pattern


def test_bridge_path_retained_despite_low_terminal_rank():
    a = _Doc("dbo.A", column_names=("Id", "BID"), confirmed_relationship_targets=("dbo.Bridge.Id",),
             confirmed_relationship_edges=(_out("BID", "dbo.Bridge.Id"),))
    bridge = _Doc("dbo.Bridge", column_names=("Id", "CID"), confirmed_relationship_sources=("dbo.A.BID",),
                  confirmed_relationship_targets=("dbo.C.Id",),
                  confirmed_relationship_edges=(_in("Id", "dbo.A.BID"), _out("CID", "dbo.C.Id")))
    c = _Doc("dbo.C", column_names=("Id",), confirmed_relationship_sources=("dbo.Bridge.CID",),
             confirmed_relationship_edges=(_in("Id", "dbo.Bridge.CID"),))
    docs = [a, bridge, c]
    # dbo.Bridge has NO independent question relevance at all (requirement
    # 4's "bridge objects need no independent question similarity") --
    # it never appears as anyone's candidate, ranked or otherwise.
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:c", [("dbo.C", 0.9)])]
    result = _plan(reqs, docs, anchor_fqns=("dbo.A", "dbo.C"))
    assert result.primary_plan.bridge_object_fqns == ("dbo.Bridge",)
    assert set(result.primary_plan.uncovered_requirement_keys) == set()


def test_frontier_limits_are_deterministic_across_repeated_calls():
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    results = [_plan(reqs, docs, anchor_fqns=anchor_fqns) for _ in range(4)]
    first = results[0]
    for r in results[1:]:
        assert r.limits_reached == first.limits_reached
        assert r.primary_plan.selected_object_fqns == first.primary_plan.selected_object_fqns
        assert r.considered_path_count == first.considered_path_count


def test_bounded_memory_and_operations_for_a_pathological_requirement_set():
    """Many requirements, each with many candidates -- must complete
    quickly and never exceed the global candidate-attempt ceiling,
    confirming the frontier remains a BOUNDED approximation, never an
    exhaustive enumeration over subsets."""
    docs = [_Doc(f"dbo.Obj{i}", column_names=("Id",)) for i in range(200)]
    reqs = [
        _requirement(f"ENTITY:concept{k}", [(f"dbo.Obj{k * 10 + j}", 0.5 + j * 0.01) for j in range(10)])
        for k in range(20)
    ]
    limits = JoinPlanningLimits(max_considered_paths=500)
    t0 = time.perf_counter()
    result = _plan(reqs, docs, limits=limits)
    elapsed = time.perf_counter() - t0
    assert result.considered_path_count <= 500
    assert elapsed < 5.0  # generous sanity bound, not a strict benchmark
    assert result.status in (STATUS_OK, STATUS_PARTIAL_COVERAGE, STATUS_LIMITS_EXCEEDED)


def test_shuffled_requirement_and_candidate_order_is_deterministic():
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    baseline = _plan(reqs, docs, anchor_fqns=anchor_fqns)

    shuffled_docs = docs[:]
    random.Random(3).shuffle(shuffled_docs)
    shuffled_reqs = [
        _requirement("ENTITY:x", list(reversed([(c.object_fqn, c.confidence) for c in reqs[1].candidates]))),
        reqs[0],
    ]
    shuffled_result = _plan(shuffled_reqs, shuffled_docs, anchor_fqns=anchor_fqns)
    assert set(baseline.primary_plan.selected_object_fqns) == set(shuffled_result.primary_plan.selected_object_fqns)
    assert baseline.primary_plan.plan_kind == shuffled_result.primary_plan.plan_kind


# ── Phase 6, Step 6G, Part 3/5: model/authority evidence stays advisory ───
#
# core.sqlservice.schema_linker.MATCH_KIND_MODEL_SEMANTIC (validated model-
# inferred evidence) and core.sqlservice.view_authority.MATCH_KIND_VIEW_
# AUTHORITY (domain/source-role/canonical-quality boost evidence) both
# populate MatchedRequirement.candidates exactly like a real structural
# match, but neither may, by itself, flip a BUSINESS_REQUIRED/FILTER_OR_
# GROUPING concept from uncovered to covered -- Step 6F's own offline
# evaluation traced a real production regression (most -> fewest required-
# object coverage on the the reference database golden cases) to exactly this gap: a single
# object nominally "covering" several concepts purely through advisory
# evidence looked "complete" without any real evidence backing it.


def _advisory_req_candidate(object_fqn: str, confidence: float, match_kind: str) -> RequirementCandidate:
    return RequirementCandidate(
        object_fqn=object_fqn, column_name=None, technical_label="x", match_kind=match_kind,
        confidence=confidence, raw_confidence=confidence, informativeness=1.0, evidence=(),
    )


def _mixed_requirement(concept_key: str, structural: list[tuple[str, float]], advisory: list[tuple[str, float, str]], strength: str | None = None) -> MatchedRequirement:
    kind, _, label = concept_key.partition(":")
    cands = tuple(_req_candidate(fqn, conf) for fqn, conf in structural)
    cands += tuple(_advisory_req_candidate(fqn, conf, mk) for fqn, conf, mk in advisory)
    if strength is None:
        strength = _DEFAULT_STRENGTH_BY_KIND.get(kind, "SUPPORTING")
    return MatchedRequirement(
        concept_kind=kind, concept_key=concept_key, normalized_label=label, matched_terms=(label,),
        candidates=cands, candidate_total_count=len(cands),
        confidence=cands[0].confidence if cands else 0.0, ambiguous=False, evidence=(), strength=strength,
    )


def test_model_only_candidate_cannot_satisfy_a_mandatory_requirement_alone():
    from core.sqlservice.schema_linker import MATCH_KIND_MODEL_SEMANTIC

    docs = [_Doc("dbo.Broad", column_names=("ID",))]
    reqs = [_mixed_requirement("MEASURE:amount", [], [("dbo.Broad", 0.5, MATCH_KIND_MODEL_SEMANTIC)])]
    result = _plan(reqs, docs, anchor_fqns=("dbo.Broad",), anchor_scores={"dbo.Broad": 0.8})
    assert "MEASURE:amount" not in result.primary_plan.covered_requirement_keys
    assert "MEASURE:amount" in result.primary_plan.uncovered_requirement_keys


def test_view_authority_only_candidate_cannot_satisfy_a_mandatory_requirement_alone():
    from core.sqlservice.view_authority import MATCH_KIND_VIEW_AUTHORITY

    docs = [_Doc("dbo.Broad", column_names=("ID",))]
    reqs = [_mixed_requirement("MEASURE:amount", [], [("dbo.Broad", 0.5, MATCH_KIND_VIEW_AUTHORITY)])]
    result = _plan(reqs, docs, anchor_fqns=("dbo.Broad",), anchor_scores={"dbo.Broad": 0.8})
    assert "MEASURE:amount" not in result.primary_plan.covered_requirement_keys
    assert "MEASURE:amount" in result.primary_plan.uncovered_requirement_keys


def test_structural_candidate_wins_coverage_credit_over_a_higher_confidence_model_candidate():
    """A weaker, but genuinely STRUCTURAL, candidate must be preferred for
    coverage-credit purposes over a stronger-looking MODEL_SEMANTIC one for
    the SAME concept -- "structural evidence outranks model evidence" even
    when the model candidate's raw confidence number is larger."""
    from core.sqlservice.schema_linker import MATCH_KIND_MODEL_SEMANTIC

    docs = [_Doc("dbo.RealMatch", column_names=("ID",)), _Doc("dbo.ModelOnly", column_names=("ID",))]
    reqs = [
        _mixed_requirement(
            "MEASURE:amount", [("dbo.RealMatch", 0.4)], [("dbo.ModelOnly", 0.9, MATCH_KIND_MODEL_SEMANTIC)],
        ),
    ]
    result = _plan(
        reqs, docs, anchor_fqns=("dbo.RealMatch", "dbo.ModelOnly"),
        anchor_scores={"dbo.RealMatch": 0.4, "dbo.ModelOnly": 0.9},
    )
    assert "MEASURE:amount" in result.primary_plan.covered_requirement_keys
    assert result.primary_plan.selected_object_fqns == ("dbo.RealMatch",)


# ── Phase 6, Step 6G, Part 4: alternative-plan diversity ──────────────────


def test_strong_base_table_plan_and_relevant_view_plan_both_offered_as_distinct_alternatives():
    """When a connected multi-table (base-table) plan and a genuinely
    relevant single-view plan both cover the same requirements, both
    belong among the offered plans (primary + alternatives) as distinct
    anchor families -- Part 4's "keep the strongest plan for each
    meaningfully distinct anchor family" / "include a strong base-table
    plan... include a business-view plan when its evidence supports it"."""
    students = _Doc(
        "dbo.Students", column_names=("StudentID",),
        confirmed_relationship_targets=("dbo.Enrollments.StudentID",),
        confirmed_relationship_edges=(_in("StudentID", "dbo.Enrollments.StudentID"),),
    )
    enrollments = _Doc(
        "dbo.Enrollments", column_names=("StudentID", "ClassID"),
        confirmed_relationship_sources=("dbo.Students.StudentID",),
        confirmed_relationship_targets=("dbo.Classes.ClassID",),
        confirmed_relationship_edges=(
            _out("StudentID", "dbo.Students.StudentID"), _out("ClassID", "dbo.Classes.ClassID"),
        ),
    )
    classes = _Doc(
        "dbo.Classes", column_names=("ClassID",),
        confirmed_relationship_sources=("dbo.Enrollments.ClassID",),
        confirmed_relationship_edges=(_in("ClassID", "dbo.Enrollments.ClassID"),),
    )
    view = _Doc("dbo.vw_StudentClassSummary", object_type="VIEW", column_names=("StudentName", "ClassName"))
    docs = [students, enrollments, classes, view]
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9), ("dbo.vw_StudentClassSummary", 0.85)]),
        _requirement("DIMENSION:class", [("dbo.Classes", 0.9), ("dbo.vw_StudentClassSummary", 0.85)]),
    ]
    result = _plan(
        reqs, docs, anchor_fqns=("dbo.Students", "dbo.Classes", "dbo.vw_StudentClassSummary"),
        anchor_scores={"dbo.Students": 0.9, "dbo.Classes": 0.9, "dbo.vw_StudentClassSummary": 0.85},
    )
    all_plans = (result.primary_plan, *result.alternative_plans)
    all_object_sets = [frozenset(p.selected_object_fqns) for p in all_plans]
    assert frozenset({"dbo.Students", "dbo.Enrollments", "dbo.Classes"}) in all_object_sets
    assert frozenset({"dbo.vw_StudentClassSummary"}) in all_object_sets
    # Distinct anchor families, never near-duplicate slots -- exactly 2
    # meaningfully different plan shapes here, not one repeated twice.
    assert len(set(all_object_sets)) == len(all_object_sets)


def test_alternative_plans_deduplicated_by_object_set():
    """No two offered plans (primary + alternatives) ever share the exact
    same selected-object set -- Part 4's "deduplicate plans by canonical
    object set... avoid filling all option slots with near-identical
    views"."""
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    result = _plan(reqs, docs, anchor_fqns=anchor_fqns)
    all_plans = (result.primary_plan, *result.alternative_plans)
    object_sets = [frozenset(p.selected_object_fqns) for p in all_plans]
    assert len(object_sets) == len(set(object_sets))


def test_partial_plan_ranked_after_a_complete_plan_when_both_are_offered():
    """A plan that leaves a mandatory requirement uncovered must never
    outrank (appear before, in offered order) a plan that covers
    everything -- Part 4's "partial plans remain clearly labeled and
    ranked after complete plans", exercised through this module's own
    unmodified `_score_state` business_ratio-first lexicographic order."""
    customers = _Doc(
        "dbo.Customers", column_names=("CustomerID",),
        confirmed_relationship_targets=("dbo.Orders.CustomerID",),
        confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),),
    )
    orders = _Doc(
        "dbo.Orders", column_names=("OrderID", "CustomerID"),
        confirmed_relationship_sources=("dbo.Customers.CustomerID",),
        confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),),
    )
    isolated = _Doc("dbo.Isolated", column_names=("IsolatedID",))
    docs = [customers, orders, isolated]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
        _requirement("ENTITY:isolated", [("dbo.Isolated", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_PARTIAL_COVERAGE
    # The complete-for-what's-connectable plan (Customers+Orders) is
    # primary; a plan settling for ONLY Isolated (dropping two covered
    # requirements to pick up one disconnected one) must never be
    # ranked ahead of it.
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders"}
    isolated_only_index = next(
        (i for i, p in enumerate((result.primary_plan, *result.alternative_plans)) if p.selected_object_fqns == ("dbo.Isolated",)),
        None,
    )
    assert isolated_only_index is None or isolated_only_index > 0


# ── Phase 6, Step 6H, Part 3/4/5: relevance-first ordering, Pareto ────────
#    dominance, and PlanScoreBreakdown decomposition


def test_stronger_relevance_beats_a_marginal_size_advantage():
    """A smaller plan must not win purely on object count when a larger,
    connected plan has MATERIALLY stronger direct relevance -- Part 3's
    core fix: "plan size may never defeat materially stronger direct
    relevance". Two REAL structural candidates for the same concept, well
    outside the ambiguity margin (0.95 vs 0.40): the weaker one reached via
    a smaller, single-object plan must lose to the stronger one even
    though the stronger one requires one more object."""
    from core.sqlservice.join_planner import _score_plan_state, _PlanState, _JoinGraphIndex, _Requirement
    from types import MappingProxyType as _MPT

    index = _JoinGraphIndex(
        fqn_by_lower=_MPT({"strong": "strong", "weak": "weak", "bridge": "bridge"}),
        eligible_fqns_lower=frozenset({"strong", "weak", "bridge"}),
        object_type_by_fqn_lower=_MPT({"strong": "TABLE", "weak": "TABLE", "bridge": "TABLE"}),
        joinable_adjacency=_MPT({"strong": (), "weak": (), "bridge": ()}),
        joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
        degree_by_fqn_lower=_MPT({"strong": 1, "weak": 1, "bridge": 1}), max_degree=1,
        canonical_score_by_fqn_lower=_MPT({"strong": 0.5, "weak": 0.5, "bridge": 0.5}),
    )
    requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED",
        candidates=(("strong", 0.95), ("weak", 0.40)),
        candidate_total_count=2, truncated_by_ceiling=False,
        structural_fqns_lower=frozenset({"strong", "weak"}),
        raw_confidence_by_fqn=_MPT({"strong": 0.95, "weak": 0.40}),
    )]
    edge = PlannedJoinEdge(
        from_object_fqn="weak", to_object_fqn="bridge", column_pairs=(("id", "id"),),
        relationship_type=RELATIONSHIP_TYPE_PHYSICAL_FK, inference_method=RELATIONSHIP_TYPE_PHYSICAL_FK,
        direction=DIRECTION_FORWARD, join_capability=JOIN_CAPABILITY_JOINABLE,
    )
    small_weak_plan = _PlanState(
        selected_lower=frozenset({"weak"}), selected_order=("weak",), edges=(),
        terminal_choice_by_concept={"ENTITY:a": "weak"},
    )
    larger_strong_plan = _PlanState(
        selected_lower=frozenset({"strong", "bridge"}), selected_order=("strong", "bridge"), edges=(edge,),
        terminal_choice_by_concept={"ENTITY:a": "strong"},
    )
    weak_breakdown = _score_plan_state(small_weak_plan, requirements, index, 12)
    strong_breakdown = _score_plan_state(larger_strong_plan, requirements, index, 12)
    assert strong_breakdown.sort_key < weak_breakdown.sort_key


def test_complete_connected_two_table_plan_beats_a_weak_single_object_plan():
    """A connected two-table plan covering BOTH mandatory concepts must
    beat a single-object plan covering only one, even though the single-
    object plan is smaller -- Part 3's "a one-object plan must not
    automatically beat a connected multi-object plan"."""
    customers = _Doc(
        "dbo.Customers", column_names=("CustomerID",),
        confirmed_relationship_targets=("dbo.Orders.CustomerID",),
        confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),),
    )
    orders = _Doc(
        "dbo.Orders", column_names=("OrderID", "CustomerID"),
        confirmed_relationship_sources=("dbo.Customers.CustomerID",),
        confirmed_relationship_edges=(_out("CustomerID", "dbo.Customers.CustomerID"),),
    )
    docs = [customers, orders]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:order", [("dbo.Orders", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders"}


def test_smaller_plan_wins_only_after_coverage_structure_relevance_connectivity_tie():
    """Once coverage, structural quality, relevance, and connectivity are
    ALL genuinely tied, the smaller plan correctly wins -- size is a real,
    intended tie-breaker, just a low-priority one."""
    from core.sqlservice.join_planner import _score_plan_state, _PlanState, _JoinGraphIndex, _Requirement
    from types import MappingProxyType as _MPT

    index = _JoinGraphIndex(
        fqn_by_lower=_MPT({"a": "a", "bridge": "bridge"}),
        eligible_fqns_lower=frozenset({"a", "bridge"}),
        object_type_by_fqn_lower=_MPT({"a": "TABLE", "bridge": "TABLE"}),
        joinable_adjacency=_MPT({"a": (), "bridge": ()}),
        joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
        degree_by_fqn_lower=_MPT({"a": 1, "bridge": 1}), max_degree=1,
        canonical_score_by_fqn_lower=_MPT({"a": 0.5, "bridge": 0.5}),
    )
    requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("a", 0.9),),
        candidate_total_count=1, truncated_by_ceiling=False,
        structural_fqns_lower=frozenset({"a"}), raw_confidence_by_fqn=_MPT({"a": 0.9}),
    )]
    small_plan = _PlanState(
        selected_lower=frozenset({"a"}), selected_order=("a",), edges=(),
        terminal_choice_by_concept={"ENTITY:a": "a"},
    )
    unnecessary_edge = PlannedJoinEdge(
        from_object_fqn="a", to_object_fqn="bridge", column_pairs=(("id", "id"),),
        relationship_type=RELATIONSHIP_TYPE_PHYSICAL_FK, inference_method=RELATIONSHIP_TYPE_PHYSICAL_FK,
        direction=DIRECTION_FORWARD, join_capability=JOIN_CAPABILITY_JOINABLE,
    )
    larger_plan_same_evidence = _PlanState(
        selected_lower=frozenset({"a", "bridge"}), selected_order=("a", "bridge"), edges=(unnecessary_edge,),
        terminal_choice_by_concept={"ENTITY:a": "a"},
    )
    small_breakdown = _score_plan_state(small_plan, requirements, index, 12)
    large_breakdown = _score_plan_state(larger_plan_same_evidence, requirements, index, 12)
    assert small_breakdown.mandatory_coverage_ratio == large_breakdown.mandatory_coverage_ratio
    assert small_breakdown.structural_quality == large_breakdown.structural_quality
    assert small_breakdown.total_direct_relevance == large_breakdown.total_direct_relevance
    assert small_breakdown.object_count < large_breakdown.object_count
    assert small_breakdown.sort_key < large_breakdown.sort_key


def test_additional_required_bridge_object_not_treated_as_irrelevant_bloat():
    """A plan needing one extra, non-terminal BRIDGE object to actually
    connect two real requirements must never be Pareto-dominated by a
    smaller, disconnected/incomplete alternative solely because of that
    extra object -- Part 4's "never remove a stronger-relevance plan
    solely because it has one additional required bridge object"."""
    customers = _Doc(
        "dbo.Customers", column_names=("CustomerID",),
        confirmed_relationship_targets=("dbo.Orders.CustomerID",),
        confirmed_relationship_edges=(_in("CustomerID", "dbo.Orders.CustomerID"),),
    )
    orders = _Doc(
        "dbo.Orders", column_names=("OrderID", "CustomerID", "ProductID"),
        confirmed_relationship_sources=("dbo.Customers.CustomerID",),
        confirmed_relationship_targets=("dbo.Products.ProductID",),
        confirmed_relationship_edges=(
            _out("CustomerID", "dbo.Customers.CustomerID"), _out("ProductID", "dbo.Products.ProductID"),
        ),
    )
    products = _Doc(
        "dbo.Products", column_names=("ProductID",),
        confirmed_relationship_sources=("dbo.Orders.ProductID",),
        confirmed_relationship_edges=(_in("ProductID", "dbo.Orders.ProductID"),),
    )
    docs = [customers, orders, products]
    reqs = [
        _requirement("ENTITY:customer", [("dbo.Customers", 0.9)]),
        _requirement("ENTITY:product", [("dbo.Products", 0.9)]),
    ]
    result = _plan(reqs, docs)
    assert result.status == STATUS_OK
    # Orders is a required BRIDGE (no requirement names it directly) but
    # it must survive Pareto filtering and be selected — it is the only
    # way to connect Customers and Products at all.
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Customers", "dbo.Orders", "dbo.Products"}
    assert "dbo.Orders" in result.primary_plan.bridge_object_fqns


def test_advisory_evidence_cannot_create_structural_coverage_in_breakdown():
    from core.sqlservice.join_planner import _score_plan_state, _PlanState, _JoinGraphIndex, _Requirement
    from types import MappingProxyType as _MPT
    from core.sqlservice.schema_linker import MATCH_KIND_MODEL_SEMANTIC

    index = _JoinGraphIndex(
        fqn_by_lower=_MPT({"broad": "broad"}), eligible_fqns_lower=frozenset({"broad"}),
        object_type_by_fqn_lower=_MPT({"broad": "VIEW"}), joinable_adjacency=_MPT({"broad": ()}),
        joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
        degree_by_fqn_lower=_MPT({"broad": 0}), max_degree=0,
        canonical_score_by_fqn_lower=_MPT({"broad": 0.5}),
    )
    requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("broad", 0.5),),
        candidate_total_count=1, truncated_by_ceiling=False,
        structural_fqns_lower=frozenset(),  # advisory-only: never in structural_fqns_lower
        raw_confidence_by_fqn=_MPT({"broad": 0.5}),
    )]
    state = _PlanState(
        selected_lower=frozenset({"broad"}), selected_order=("broad",), edges=(),
        terminal_choice_by_concept={"ENTITY:a": "broad"},
    )
    breakdown = _score_plan_state(state, requirements, index, 12)
    assert breakdown.mandatory_covered_count == 0
    assert breakdown.mandatory_coverage_ratio == 0.0
    assert "ENTITY:a" not in breakdown.covered_concept_keys
    assert breakdown.advisory_contribution > 0.0  # still visible, just never as coverage


def test_broad_semantic_view_cannot_beat_stronger_direct_evidence_in_ordering():
    """A view whose only claim to a concept is broad, capped model-
    semantic evidence must sort strictly WORSE than a narrower object with
    real, direct structural/lexical evidence for the SAME concept."""
    from core.sqlservice.join_planner import _score_plan_state, _PlanState, _JoinGraphIndex, _Requirement
    from types import MappingProxyType as _MPT
    from core.sqlservice.schema_linker import MATCH_KIND_MODEL_SEMANTIC

    index = _JoinGraphIndex(
        fqn_by_lower=_MPT({"strongtable": "strongtable", "broadview": "broadview"}),
        eligible_fqns_lower=frozenset({"strongtable", "broadview"}),
        object_type_by_fqn_lower=_MPT({"strongtable": "TABLE", "broadview": "VIEW"}),
        joinable_adjacency=_MPT({"strongtable": (), "broadview": ()}),
        joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
        degree_by_fqn_lower=_MPT({"strongtable": 0, "broadview": 0}), max_degree=0,
        canonical_score_by_fqn_lower=_MPT({"strongtable": 0.5, "broadview": 0.5}),
    )
    requirements = [_Requirement(
        concept_key="ENTITY:a", role="BUSINESS_REQUIRED",
        candidates=(("strongtable", 0.9), ("broadview", 0.5)),
        candidate_total_count=2, truncated_by_ceiling=False,
        structural_fqns_lower=frozenset({"strongtable"}),  # broadview's candidacy is advisory-only
        raw_confidence_by_fqn=_MPT({"strongtable": 0.9}),
    )]
    strong_state = _PlanState(
        selected_lower=frozenset({"strongtable"}), selected_order=("strongtable",), edges=(),
        terminal_choice_by_concept={"ENTITY:a": "strongtable"},
    )
    broad_state = _PlanState(
        selected_lower=frozenset({"broadview"}), selected_order=("broadview",), edges=(),
        terminal_choice_by_concept={"ENTITY:a": "broadview"},
    )
    strong_breakdown = _score_plan_state(strong_state, requirements, index, 12)
    broad_breakdown = _score_plan_state(broad_state, requirements, index, 12)
    assert strong_breakdown.sort_key < broad_breakdown.sort_key
    assert _dominates_public(strong_breakdown, broad_breakdown)


def _dominates_public(a, b):
    from core.sqlservice.join_planner import _dominates
    return _dominates(a, b)


def test_pareto_dominated_plan_is_removed_from_offered_alternatives():
    """A plan strictly (beyond the ambiguity margin) worse on every one of
    the four dominance dimensions must not appear among offered plans at
    all -- Part 4's own removal rule."""
    strong = _Doc(
        "dbo.Strong", column_names=("Id",),
    )
    weak = _Doc(
        "dbo.Weak", column_names=("Id",),
    )
    docs = [strong, weak]
    reqs = [_requirement("ENTITY:a", [("dbo.Strong", 0.95), ("dbo.Weak", 0.20)])]
    result = _plan(reqs, docs, anchor_fqns=("dbo.Strong", "dbo.Weak"), anchor_scores={"dbo.Strong": 0.95, "dbo.Weak": 0.20})
    all_plans = (result.primary_plan, *result.alternative_plans)
    assert ("dbo.Weak",) not in [p.selected_object_fqns for p in all_plans]


def test_non_dominated_alternative_survives_pareto_filtering():
    """Two candidates within the ambiguity margin of each other for the
    SAME concept must both survive as offered plans -- neither dominates
    the other under the margin-tolerant comparison."""
    a = _Doc("dbo.A", column_names=("Id", "B1ID", "B2ID"),
             confirmed_relationship_targets=("dbo.B1.Id", "dbo.B2.Id"),
             confirmed_relationship_edges=(_out("B1ID", "dbo.B1.Id"), _out("B2ID", "dbo.B2.Id")))
    b1 = _Doc("dbo.B1", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B1ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B1ID"),))
    b2 = _Doc("dbo.B2", column_names=("Id",), confirmed_relationship_sources=("dbo.A.B2ID",),
              confirmed_relationship_edges=(_in("Id", "dbo.A.B2ID"),))
    docs = [a, b1, b2]
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.9)]),
        _requirement("ENTITY:b", [("dbo.B1", 0.8), ("dbo.B2", 0.78)]),
    ]
    result = _plan(reqs, docs)
    all_object_sets = [frozenset(p.selected_object_fqns) for p in (result.primary_plan, *result.alternative_plans)]
    assert frozenset({"dbo.A", "dbo.B1"}) in all_object_sets
    assert frozenset({"dbo.A", "dbo.B2"}) in all_object_sets


def test_deterministic_ordering_repeated_execution_byte_identical():
    reqs, docs, anchor_fqns = _x_y_crowding_fixture()
    result1 = _plan(reqs, docs, anchor_fqns=anchor_fqns)
    result2 = _plan(reqs, docs, anchor_fqns=anchor_fqns)
    assert result1.primary_plan.selected_object_fqns == result2.primary_plan.selected_object_fqns
    assert [p.selected_object_fqns for p in result1.alternative_plans] == [p.selected_object_fqns for p in result2.alternative_plans]


def test_ordering_invariant_under_object_renaming():
    """The corrected sort_key must be a pure function of scores/structure,
    never of an object's own name or how it sorts alphabetically."""
    from core.sqlservice.join_planner import _score_plan_state, _PlanState, _JoinGraphIndex, _Requirement
    from types import MappingProxyType as _MPT

    def _make(strong_name, weak_name):
        index = _JoinGraphIndex(
            fqn_by_lower=_MPT({strong_name: strong_name, weak_name: weak_name}),
            eligible_fqns_lower=frozenset({strong_name, weak_name}),
            object_type_by_fqn_lower=_MPT({strong_name: "TABLE", weak_name: "TABLE"}),
            joinable_adjacency=_MPT({strong_name: (), weak_name: ()}),
            joinable_edges=(), lineage_edges=(), unusable_edge_count=0,
            degree_by_fqn_lower=_MPT({strong_name: 0, weak_name: 0}), max_degree=0,
            canonical_score_by_fqn_lower=_MPT({strong_name: 0.5, weak_name: 0.5}),
        )
        requirements = [_Requirement(
            concept_key="ENTITY:a", role="BUSINESS_REQUIRED",
            candidates=((strong_name, 0.9), (weak_name, 0.3)),
            candidate_total_count=2, truncated_by_ceiling=False,
            structural_fqns_lower=frozenset({strong_name, weak_name}),
            raw_confidence_by_fqn=_MPT({strong_name: 0.9, weak_name: 0.3}),
        )]
        strong_state = _PlanState(
            selected_lower=frozenset({strong_name}), selected_order=(strong_name,), edges=(),
            terminal_choice_by_concept={"ENTITY:a": strong_name},
        )
        weak_state = _PlanState(
            selected_lower=frozenset({weak_name}), selected_order=(weak_name,), edges=(),
            terminal_choice_by_concept={"ENTITY:a": weak_name},
        )
        return (
            _score_plan_state(strong_state, requirements, index, 12),
            _score_plan_state(weak_state, requirements, index, 12),
        )

    strong1, weak1 = _make("alpha123", "zzztable")  # "zzz..." would win an alphabetical tiebreak
    strong2, weak2 = _make("zzznamed", "aaaearly")  # names swapped in sort order
    assert strong1.sort_key < weak1.sort_key
    assert strong2.sort_key < weak2.sort_key


def test_no_reference_specific_behavior_step3f():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "join_planner.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source
    # The old flat global beam_width field is gone -- replaced by the two
    # coverage-signature-frontier bounds (Part 3).
    assert not hasattr(JoinPlanningLimits(), "beam_width")
    assert hasattr(JoinPlanningLimits(), "max_frontier_signatures")
    assert hasattr(JoinPlanningLimits(), "max_states_per_signature")
