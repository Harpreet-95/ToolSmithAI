"""
Focused unit tests — Phase 6, Step 6J, Parts 2/3: relationship-aware bridge
candidate generation and the deterministic coverage-preserving frontier
(core/sqlservice/join_planner.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Mirrors tests/test_sqlservice_join_planner.py's own
fixture-building conventions (duplicated locally, not imported, per this
codebase's established per-file convention).

Run:
    venv/Scripts/pytest tests/test_sqlservice_join_planner_bridge_and_deterministic_frontier.py -v
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from core.sqlservice.schema_linker import (
    AnchorCandidate,
    MatchedRequirement,
    QuestionRequirements,
    RequirementCandidate,
    SchemaLinkingResult,
)
from core.sqlservice.join_planner import (
    CONCEPT_KIND_BRIDGE,
    LIMIT_MAX_BRIDGE_ANCHOR_PAIRS,
    LIMIT_MAX_BRIDGE_CANDIDATES_PER_PAIR,
    LIMIT_MAX_EXPANDED_STATES,
    LIMIT_MAX_PATHS_PER_ANCHOR_FAMILY,
    LIMIT_OPERATION_TIME_BUDGET,
    RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY,
    RELATIONSHIP_TYPE_PHYSICAL_FK,
    STATUS_OK,
    STATUS_PARTIAL_COVERAGE,
    DEFAULT_JOIN_PLANNING_LIMITS,
    JoinPlanningLimits,
    PlanScoreBreakdown,
    clear_join_planner_cache,
    plan_schema_join_subgraph,
)
import core.sqlservice.join_planner as join_planner_module
from core.sqlservice.join_planner import _dominates, _generate_bridge_requirements, _strong_requirement_anchors

# ── Fixture builders (mirrors test_sqlservice_join_planner.py) ────────────

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


def _requirement(concept_key: str, candidates: list[tuple[str, float]], strength: str = "CORE") -> MatchedRequirement:
    kind, _, label = concept_key.partition(":")
    cands = tuple(_req_candidate(fqn, conf) for fqn, conf in candidates)
    return MatchedRequirement(
        concept_kind=kind, concept_key=concept_key, normalized_label=label, matched_terms=(label,),
        candidates=cands, candidate_total_count=len(cands),
        confidence=cands[0].confidence if cands else 0.0, ambiguous=False, evidence=(), strength=strength,
    )


def _linking_result(requirements: list[MatchedRequirement], anchor_fqns: tuple[str, ...] = ()) -> SchemaLinkingResult:
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
    anchors = tuple(
        AnchorCandidate(
            object_fqn=fqn, object_type="TABLE", retrieval_tier=1, supporting_concepts=(), supporting_columns=(),
            semantic_score=None, lexical_score=None, catalog_score=0.5, example_score=0.0, canonical_score=0.5,
            model_semantic_score=0.0, combined_score=0.5, evidence=(), confidence=0.5,
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


def _plan(requirements, docs, anchor_fqns=(), limits=None):
    link = _linking_result(requirements, anchor_fqns)
    kwargs = {"limits": limits} if limits is not None else {}
    return plan_schema_join_subgraph(link, docs, **kwargs)


def _junction_schema(*, students_tier=1, signups_tier=1, classes_tier=1, edge_kind=RELATIONSHIP_TYPE_PHYSICAL_FK):
    """Students <-> Signups <-> Classes -- a genuine junction table with NO
    direct Students<->Classes edge, and NO requirement candidate of its
    own (representing zero lexical/semantic match to the question)."""
    students = _Doc(
        "dbo.Students", retrieval_tier=students_tier, column_names=("StudentID",),
        confirmed_relationship_sources=("dbo.Signups.StudentID",),
        confirmed_relationship_edges=(_in("StudentID", "dbo.Signups.StudentID", kind=edge_kind),),
    )
    signups = _Doc(
        "dbo.Signups", retrieval_tier=signups_tier, column_names=("StudentID", "ClassID"),
        confirmed_relationship_targets=("dbo.Students.StudentID", "dbo.Classes.ClassID"),
        confirmed_relationship_edges=(
            _out("StudentID", "dbo.Students.StudentID", kind=edge_kind),
            _out("ClassID", "dbo.Classes.ClassID", kind=edge_kind),
        ),
    )
    classes = _Doc(
        "dbo.Classes", retrieval_tier=classes_tier, column_names=("ClassID",),
        confirmed_relationship_sources=("dbo.Signups.ClassID",),
        confirmed_relationship_edges=(_in("ClassID", "dbo.Signups.ClassID", kind=edge_kind),),
    )
    return students, signups, classes


# ── 1. Junction table with no lexical match is included as a bridge ───────


def test_junction_table_with_no_lexical_match_is_included_as_bridge():
    students, signups, classes = _junction_schema()
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    result = _plan(reqs, [students, signups, classes])
    assert result.status == STATUS_OK
    assert "dbo.Signups" in result.primary_plan.selected_object_fqns
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Students", "dbo.Signups", "dbo.Classes"}
    # Signups never independently satisfies a real business concept -- it
    # must never appear in covered_requirement_keys, only as connective
    # tissue (bridge_object_fqns).
    assert "dbo.Signups" in result.primary_plan.bridge_object_fqns


def test_bridge_requirement_generated_directly_has_join_hint_role_and_bridge_provenance():
    from core.sqlservice.join_planner import _JoinGraphIndex, _build_join_graph_index, REQUIREMENT_ROLE_JOIN_HINT, _Requirement

    students, signups, classes = _junction_schema()
    docs = [students, signups, classes]
    index = _build_join_graph_index(docs)
    requirements = [
        _Requirement(
            concept_key="ENTITY:student", role="BUSINESS_REQUIRED", candidates=(("dbo.students", 0.9),),
            candidate_total_count=1, truncated_by_ceiling=False,
        ),
        _Requirement(
            concept_key="ENTITY:class", role="BUSINESS_REQUIRED", candidates=(("dbo.classes", 0.9),),
            candidate_total_count=1, truncated_by_ceiling=False,
        ),
    ]
    limits_reached: list[str] = []
    bridges = _generate_bridge_requirements(requirements, index, DEFAULT_JOIN_PLANNING_LIMITS, limits_reached)
    assert len(bridges) == 1
    bridge = bridges[0]
    assert bridge.concept_key.startswith(f"{CONCEPT_KIND_BRIDGE}:")
    assert bridge.role == REQUIREMENT_ROLE_JOIN_HINT
    assert bridge.candidates == (("dbo.signups", pytest.approx(0.25)),)


# ── 2. Unrelated graph neighbors are not admitted ──────────────────────────


def test_unrelated_object_with_no_confirmed_path_is_not_admitted_as_bridge():
    students, signups, classes = _junction_schema()
    unrelated = _Doc("dbo.Unrelated", column_names=("Id",))  # no confirmed relationship to anything
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    result = _plan(reqs, [students, signups, classes, unrelated])
    assert "dbo.Unrelated" not in result.primary_plan.selected_object_fqns


def test_direct_connection_needs_no_bridge_candidate():
    a = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_sources=("dbo.B.AId",),
             confirmed_relationship_edges=(_in("Id", "dbo.B.AId"),))
    b = _Doc("dbo.B", column_names=("AId",), confirmed_relationship_targets=("dbo.A.Id",),
             confirmed_relationship_edges=(_out("AId", "dbo.A.Id"),))
    from core.sqlservice.join_planner import _build_join_graph_index, _Requirement
    index = _build_join_graph_index([a, b])
    requirements = [
        _Requirement(concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("dbo.a", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
        _Requirement(concept_key="ENTITY:b", role="BUSINESS_REQUIRED", candidates=(("dbo.b", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
    ]
    bridges = _generate_bridge_requirements(requirements, index, DEFAULT_JOIN_PLANNING_LIMITS, [])
    assert bridges == []


# ── 3. Lineage-only edges cannot create executable bridges ────────────────


def test_lineage_only_edges_cannot_create_bridge():
    students, signups, classes = _junction_schema(edge_kind=RELATIONSHIP_TYPE_CATALOG_VIEW_DEPENDENCY)
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    result = _plan(reqs, [students, signups, classes])
    # No JOINABLE path connects Students and Classes at all (every edge is
    # lineage-only) -- Signups must never be pulled in as a fabricated join.
    assert "dbo.Signups" not in result.primary_plan.selected_object_fqns


# ── 4. Inferred/weak edges cannot create bridges ───────────────────────────


def test_inferred_only_relationships_cannot_create_bridges():
    """This module has no notion of an inferred edge at all -- only
    confirmed_relationship_edges ever reaches the graph. A document whose
    ONLY relationship evidence is inferred_relationship_* must behave
    identically to one with no relationship evidence whatsoever."""
    students = _Doc("dbo.Students", column_names=("StudentID",),
                     inferred_relationship_targets=("dbo.Signups.StudentID",))
    signups = _Doc("dbo.Signups", column_names=("StudentID", "ClassID"),
                    inferred_relationship_sources=("dbo.Students.StudentID",),
                    inferred_relationship_targets=("dbo.Classes.ClassID",))
    classes = _Doc("dbo.Classes", column_names=("ClassID",),
                    inferred_relationship_sources=("dbo.Signups.ClassID",))
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    result = _plan(reqs, [students, signups, classes])
    assert "dbo.Signups" not in result.primary_plan.selected_object_fqns


# ── 5. Tier 3/4 objects remain excluded from bridges ───────────────────────


def test_tier_3_and_4_bridge_object_remains_excluded():
    students, signups, classes = _junction_schema(signups_tier=4)
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    result = _plan(reqs, [students, signups, classes])
    assert "dbo.Signups" not in result.primary_plan.selected_object_fqns


# ── 6. Bounded one-hop and multi-hop expansion ─────────────────────────────


def test_one_hop_bridge_expansion():
    students, signups, classes = _junction_schema()
    from core.sqlservice.join_planner import _build_join_graph_index, _Requirement
    index = _build_join_graph_index([students, signups, classes])
    requirements = [
        _Requirement(concept_key="ENTITY:student", role="BUSINESS_REQUIRED", candidates=(("dbo.students", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
        _Requirement(concept_key="ENTITY:class", role="BUSINESS_REQUIRED", candidates=(("dbo.classes", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
    ]
    bridges = _generate_bridge_requirements(requirements, index, DEFAULT_JOIN_PLANNING_LIMITS, [])
    assert len(bridges) == 1
    assert bridges[0].candidates[0][0] == "dbo.signups"


def test_multi_hop_bridge_respects_max_path_hops():
    # A -> Bridge1 -> Bridge2 -> B, a 3-edge chain.
    a = _Doc("dbo.A", column_names=("Id",), confirmed_relationship_sources=("dbo.Bridge1.AId",),
             confirmed_relationship_edges=(_in("Id", "dbo.Bridge1.AId"),))
    bridge1 = _Doc("dbo.Bridge1", column_names=("AId", "B1Id"),
                    confirmed_relationship_targets=("dbo.A.Id",), confirmed_relationship_sources=("dbo.Bridge2.B1Id",),
                    confirmed_relationship_edges=(_out("AId", "dbo.A.Id"), _in("B1Id", "dbo.Bridge2.B1Id")))
    bridge2 = _Doc("dbo.Bridge2", column_names=("B1Id", "BId"),
                    confirmed_relationship_targets=("dbo.Bridge1.B1Id",), confirmed_relationship_sources=("dbo.B.BId",),
                    confirmed_relationship_edges=(_out("B1Id", "dbo.Bridge1.B1Id"), _in("BId", "dbo.B.BId")))
    b = _Doc("dbo.B", column_names=("BId",), confirmed_relationship_targets=("dbo.Bridge2.BId",),
             confirmed_relationship_edges=(_out("BId", "dbo.Bridge2.BId"),))
    docs = [a, bridge1, bridge2, b]
    from core.sqlservice.join_planner import _build_join_graph_index, _Requirement
    index = _build_join_graph_index(docs)
    requirements = [
        _Requirement(concept_key="ENTITY:a", role="BUSINESS_REQUIRED", candidates=(("dbo.a", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
        _Requirement(concept_key="ENTITY:b", role="BUSINESS_REQUIRED", candidates=(("dbo.b", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False),
    ]
    # max_path_hops=3 (the default) is exactly enough for a 3-edge chain.
    within_bound = _generate_bridge_requirements(requirements, index, DEFAULT_JOIN_PLANNING_LIMITS, [])
    assert len(within_bound) == 1
    assert set(fqn for fqn, _c in within_bound[0].candidates) == {"dbo.bridge1", "dbo.bridge2"}

    # A tighter hop bound (2) cannot reach across a 3-edge chain at all.
    tight_limits = JoinPlanningLimits(max_path_hops=2)
    beyond_bound = _generate_bridge_requirements(requirements, index, tight_limits, [])
    assert beyond_bound == []


def test_bridge_anchor_pairs_bounded_no_global_flood(monkeypatch):
    """Many BUSINESS_REQUIRED requirements (many anchor pairs) must never
    explode into an unbounded number of examined pairs."""
    from core.sqlservice.join_planner import _build_join_graph_index, _Requirement

    docs = [_Doc(f"dbo.T{i}", column_names=("Id",)) for i in range(8)]
    index = _build_join_graph_index(docs)
    requirements = [
        _Requirement(concept_key=f"ENTITY:t{i}", role="BUSINESS_REQUIRED", candidates=((f"dbo.t{i}", 0.9),),
                     candidate_total_count=1, truncated_by_ceiling=False)
        for i in range(8)
    ]
    tiny_limits = JoinPlanningLimits(max_bridge_anchor_pairs=2)
    limits_reached: list[str] = []
    _generate_bridge_requirements(requirements, index, tiny_limits, limits_reached)
    assert LIMIT_MAX_BRIDGE_ANCHOR_PAIRS in limits_reached


# ── 7. Deterministic bridge ordering ───────────────────────────────────────


def test_bridge_candidate_ordering_is_deterministic_regardless_of_document_order():
    students, signups, classes = _junction_schema()
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    first = _plan(reqs, [students, signups, classes])
    second = _plan(reqs, [classes, students, signups])
    assert first.primary_plan.selected_object_fqns == second.primary_plan.selected_object_fqns


# ── 8/9. Coverage-signature capacity: one interpretation cannot starve another ──


def test_distinct_coverage_signatures_each_retain_frontier_capacity():
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))
    reqs = [
        _requirement("ENTITY:a", [("dbo.A", 0.95)]),
        _requirement("ENTITY:b", [("dbo.B", 0.55)]),
    ]
    result = _plan(reqs, [a, b])
    covered_keys = set(result.primary_plan.covered_requirement_keys) | set(
        k for alt in result.alternative_plans for k in alt.covered_requirement_keys
    )
    assert "ENTITY:a" in covered_keys or "ENTITY:b" in covered_keys


def test_one_high_scoring_anchor_family_cannot_starve_another():
    """Many near-identical high-scoring extensions of ONE anchor family
    must not consume the whole per-round expansion budget before a
    genuinely different, lower-scoring anchor family is even attempted."""
    strong_family_docs = [_Doc(f"dbo.Strong{i}", column_names=("Id",)) for i in range(30)]
    weak_target = _Doc("dbo.WeakButRequired", column_names=("Id",))
    docs = [*strong_family_docs, weak_target]
    reqs = [
        _requirement("ENTITY:strong", [(d.object_fqn, 0.9 - i * 0.001) for i, d in enumerate(strong_family_docs)]),
        _requirement("ENTITY:weak", [("dbo.WeakButRequired", 0.15)]),
    ]
    tight_limits = JoinPlanningLimits(max_paths_per_anchor_family=3, max_expanded_states=500)
    result = _plan(reqs, docs, limits=tight_limits)
    covered_keys = set(result.primary_plan.covered_requirement_keys) | set(
        k for alt in result.alternative_plans for k in alt.covered_requirement_keys
    )
    assert "ENTITY:weak" in covered_keys, (
        "the weak-but-required concept must still be reachable even when a much larger "
        "competing anchor family exists"
    )


# ── 10/11. Pareto dominance only within compatible coverage signatures ────


def _breakdown(mandatory_ratio, structural_quality, relevance, connectivity, covered_keys):
    return PlanScoreBreakdown(
        mandatory_total_count=1, mandatory_covered_count=1, mandatory_coverage_ratio=mandatory_ratio,
        optional_total_count=0, optional_covered_count=0, optional_coverage_ratio=1.0,
        structural_covered_total=len(covered_keys), structural_quality=structural_quality,
        min_direct_relevance_mandatory=relevance, total_direct_relevance=relevance,
        direct_relevance_by_concept=MappingProxyType({}), strongest_evidence_kind_by_concept=MappingProxyType({}),
        connectivity_quality=connectivity, advisory_contribution=0.0, join_count=0, object_count=1,
        canonical_quality=0.5, hub_quality=1.0, covered_concept_keys=frozenset(covered_keys),
        aggregate_score=1.0, confidence=0.9, sort_key=(),
    )


def test_pareto_dominance_never_crosses_incompatible_coverage_signatures():
    # Both score identically high on every numeric dimension, but cover
    # completely DIFFERENT required concepts -- neither may dominate the
    # other, regardless of numeric ties.
    a = _breakdown(1.0, 1.0, 1.0, 1.0, {"ENTITY:alpha"})
    b = _breakdown(1.0, 1.0, 1.0, 1.0, {"ENTITY:beta"})
    assert not _dominates(a, b)
    assert not _dominates(b, a)


def test_pareto_dominance_still_applies_within_a_compatible_superset_signature():
    # `a` covers everything `b` covers, PLUS more, and is materially
    # better on a real dimension -- domination is still expected.
    a = _breakdown(1.0, 1.0, 1.0, 1.0, {"ENTITY:alpha", "ENTITY:beta"})
    b = _breakdown(0.5, 0.5, 0.5, 0.5, {"ENTITY:alpha"})
    assert _dominates(a, b)


def test_lower_relevance_required_bridge_plan_survives_a_higher_relevance_unrelated_plan():
    """A plan covering a genuinely different, lower-confidence required
    concept must never be discarded merely because a DIFFERENT plan
    scores higher on average relevance for a DIFFERENT concept."""
    weak_target = _Doc("dbo.RareRequired", column_names=("Id",))
    strong_target = _Doc("dbo.PopularOther", column_names=("Id",))
    docs = [weak_target, strong_target]
    reqs = [
        _requirement("ENTITY:rare", [("dbo.RareRequired", 0.2)]),
        _requirement("ENTITY:popular", [("dbo.PopularOther", 0.95)]),
    ]
    result = _plan(reqs, docs)
    all_plans = (result.primary_plan, *result.alternative_plans)
    assert any("dbo.RareRequired" in p.selected_object_fqns for p in all_plans), (
        "the low-confidence but genuinely required object must survive as at least one offered plan"
    )


# ── 12/13. Deterministic repeats under artificial timing variation ────────


def test_repeated_runs_identical_under_simulated_clock_variation(monkeypatch):
    students, signups, classes = _junction_schema()
    reqs = [
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("ENTITY:class", [("dbo.Classes", 0.9)]),
    ]
    docs = [students, signups, classes]

    baseline = _plan(reqs, docs)

    # Simulate a slow/loaded CPU: perf_counter() advances by a large,
    # varying amount on every call. If the search still relied on wall-
    # clock as its practical bound, this would visibly truncate rounds
    # differently across repeated calls.
    real_counter = __import__("time").perf_counter
    call_count = {"n": 0}

    def _jittery_counter():
        call_count["n"] += 1
        return real_counter() + call_count["n"] * 0.001  # small, varying artificial drift

    monkeypatch.setattr(join_planner_module.time, "perf_counter", _jittery_counter)
    jittered = _plan(reqs, docs)

    assert jittered.primary_plan.selected_object_fqns == baseline.primary_plan.selected_object_fqns
    assert jittered.status == baseline.status


def test_max_expanded_states_limit_is_deterministic_across_repeated_calls():
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.8)])]
    tiny_limits = JoinPlanningLimits(max_expanded_states=1)
    first = _plan(reqs, [a, b], limits=tiny_limits)
    second = _plan(reqs, [a, b], limits=tiny_limits)
    assert LIMIT_MAX_EXPANDED_STATES in first.limits_reached
    assert first.primary_plan.selected_object_fqns == second.primary_plan.selected_object_fqns
    assert first.limits_reached == second.limits_reached


# ── 14. Emergency time guard returns a typed incomplete result ────────────


def test_emergency_time_guard_returns_typed_partial_status(monkeypatch):
    a = _Doc("dbo.A", column_names=("Id",))
    b = _Doc("dbo.B", column_names=("Id",))
    reqs = [_requirement("ENTITY:a", [("dbo.A", 0.9)]), _requirement("ENTITY:b", [("dbo.B", 0.8)])]
    # A budget so small the FIRST wall-clock check already exceeds it --
    # this is the "emergency" boundary firing, not the practical bound.
    tiny_time_limits = JoinPlanningLimits(operation_time_budget_ms=-1.0)
    result = _plan(reqs, [a, b], limits=tiny_time_limits)
    assert LIMIT_OPERATION_TIME_BUDGET in result.limits_reached
    assert result.status in (STATUS_OK, STATUS_PARTIAL_COVERAGE)
    # Never silently claim a complete search happened -- coverage must be
    # visibly incomplete when nothing at all could be searched, UNLESS a
    # single-object baseline alone already answers it (SINGLE_OBJECT
    # plans bypass the beam search entirely, by design).
    assert result.primary_plan is not None


# ── Renaming invariance ─────────────────────────────────────────────────────


def test_bridge_generation_is_invariant_under_object_renaming():
    def _run(student_name, signup_name, class_name):
        students = _Doc(student_name, column_names=("StudentID",),
                        confirmed_relationship_sources=(f"{signup_name}.StudentID",),
                        confirmed_relationship_edges=(_in("StudentID", f"{signup_name}.StudentID"),))
        signups = _Doc(signup_name, column_names=("StudentID", "ClassID"),
                       confirmed_relationship_targets=(f"{student_name}.StudentID", f"{class_name}.ClassID"),
                       confirmed_relationship_edges=(
                           _out("StudentID", f"{student_name}.StudentID"),
                           _out("ClassID", f"{class_name}.ClassID"),
                       ))
        classes = _Doc(class_name, column_names=("ClassID",),
                       confirmed_relationship_sources=(f"{signup_name}.ClassID",),
                       confirmed_relationship_edges=(_in("ClassID", f"{signup_name}.ClassID"),))
        reqs = [
            _requirement("ENTITY:student", [(student_name, 0.9)]),
            _requirement("ENTITY:class", [(class_name, 0.9)]),
        ]
        return _plan(reqs, [students, signups, classes])

    result_a = _run("dbo.Students", "dbo.Signups", "dbo.Classes")
    result_b = _run("zzz.CompletelyDifferentAlpha", "zzz.CompletelyDifferentBeta", "zzz.CompletelyDifferentGamma")

    assert set(result_a.primary_plan.selected_object_fqns) == {"dbo.Students", "dbo.Signups", "dbo.Classes"}
    assert set(result_b.primary_plan.selected_object_fqns) == {
        "zzz.CompletelyDifferentAlpha", "zzz.CompletelyDifferentBeta", "zzz.CompletelyDifferentGamma",
    }
