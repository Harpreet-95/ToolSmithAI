"""
Focused unit tests — view-first business-source Step:
core/sqlservice/view_authority.py.

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Small synthetic fixtures only.

Run:
    venv/Scripts/pytest tests/test_sqlservice_view_authority.py -v
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import MappingProxyType

import pytest

from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, clear_canonical_scoring_cache
from core.sqlservice.join_planner import (
    PLAN_KIND_JOINED_SUBGRAPH, STATUS_OK as PLANNER_OK, clear_join_planner_cache,
    plan_schema_join_subgraph,
)
from core.sqlservice.schema_linker import (
    AnchorCandidate, CONCEPT_KIND_DIMENSION, CONCEPT_KIND_ENTITY, MatchedRequirement,
    QuestionRequirements, RequirementCandidate, STRENGTH_CORE, SchemaLinkingResult,
)
import core.sqlservice.view_authority as subject

# ── Fixture builders (mirrors tests/test_sqlservice_join_planner.py's own
# conventions so a reader familiar with that file recognizes this one) ────

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


def _out(column: str, other: str, kind: str = "PHYSICAL_FK") -> dict:
    return {"direction": "outbound", "column": column, "other": other, "kind": kind}


def _in(column: str, other: str, kind: str = "PHYSICAL_FK") -> dict:
    return {"direction": "inbound", "column": column, "other": other, "kind": kind}


def _req_candidate(object_fqn: str, confidence: float = 0.9) -> RequirementCandidate:
    return RequirementCandidate(
        object_fqn=object_fqn, column_name=None, technical_label="x", match_kind="COLUMN_TOKEN",
        confidence=confidence, raw_confidence=confidence, informativeness=1.0, evidence=(),
    )


def _requirement(concept_key: str, candidates: list) -> MatchedRequirement:
    kind, _, label = concept_key.partition(":")
    cands = tuple(_req_candidate(fqn, conf) for fqn, conf in candidates)
    return MatchedRequirement(
        concept_kind=kind, concept_key=concept_key, normalized_label=label, matched_terms=(label,),
        candidates=cands, candidate_total_count=len(cands),
        confidence=cands[0].confidence if cands else 0.0, ambiguous=False, evidence=(), strength=STRENGTH_CORE,
    )


def _linking(requirements: list) -> SchemaLinkingResult:
    by_kind = {"ENTITY": [], "MEASURE": [], "DIMENSION": [], "DATE_ROLE": [], "IDENTIFIER": []}
    for r in requirements:
        by_kind[r.concept_kind].append(r)
    qr = QuestionRequirements(
        operation="UNKNOWN", entity_concepts=tuple(by_kind["ENTITY"]), measure_concepts=tuple(by_kind["MEASURE"]),
        dimension_concepts=tuple(by_kind["DIMENSION"]), date_role_concepts=tuple(by_kind["DATE_ROLE"]),
        identifier_concepts=tuple(by_kind["IDENTIFIER"]), wants_comparison=False, wants_trend=False,
        wants_grouping=False, unmatched_terms=(), unresolved_filter_values=(), ambiguity_signals=(),
    )
    return SchemaLinkingResult(
        status="OK", requirements=qr, anchors=(), ambiguities=(), warnings=(), fingerprint="fp",
        eligible_object_count=0, considered_object_count=0, anchor_total_count=0, elapsed_ms=0.0,
        relevance_qualified_object_count=0, rejected_for_no_relevance_count=0, anchor_selected_count=0,
        anchors_rejected_by_confidence=0, anchor_ceiling_reached=False, no_confident_anchor_reason=None,
        raw_catalog_match_count=0, normalized_requirement_count=len(requirements), duplicate_matches_merged_count=0,
        common_token_matches_downweighted_count=0, candidate_alternatives_retained_count=0,
        anchors_selected_by_independent_evidence_count=0, anchors_retained_for_ambiguity_count=0,
    )


@pytest.fixture(autouse=True)
def _isolated_caches():
    subject.clear_view_authority_cache()
    clear_canonical_scoring_cache()
    clear_join_planner_cache()
    yield
    subject.clear_view_authority_cache()
    clear_canonical_scoring_cache()
    clear_join_planner_cache()


# ── 1/5. A view whose ONLY evidence is authority never wins a mandatory
#         requirement outright; a view with GENUINE structural evidence,
#         boosted by authority, legitimately can ─────────────────────────


def test_authority_only_view_is_offered_but_never_wins_a_mandatory_requirement_alone():
    """Phase 6, Step 6G, Part 3 correction: view_authority may ADD a
    candidate (so the view is visible/offered, e.g. as an alternative
    plan), but "domain and source-role authority must not introduce an
    object into the candidate pool by themselves" — when the view's ONLY
    candidacy for a CORE (mandatory) requirement is MATCH_KIND_VIEW_
    AUTHORITY (never any real structural/dictionary evidence for that
    concept), it must never independently satisfy that requirement well
    enough to beat a plan built from real structural candidates. This
    replaces this test's own pre-Step-6G version, which asserted the
    opposite (the view unconditionally wins) — that assertion was exactly
    the production defect Step 6F's own offline evaluation traced a real
    required-object-coverage regression to (most -> fewest on the the reference database
    golden cases): a view could look "complete" purely through advisory
    authority evidence nobody had actually verified for these specific
    concepts."""
    students = _Doc(
        "dbo.Students", column_names=("StudentID",), primary_key_columns=("StudentID",), has_primary_key=True,
        confirmed_relationship_targets=("dbo.Enrollments.StudentID",),
        confirmed_relationship_edges=(_in("StudentID", "dbo.Enrollments.StudentID"),),
    )
    enrollments = _Doc(
        "dbo.Enrollments", column_names=("StudentID", "ClassID"),
        confirmed_relationship_sources=("dbo.Students.StudentID",), confirmed_relationship_targets=("dbo.Classes.ClassID",),
        confirmed_relationship_edges=(
            _out("StudentID", "dbo.Students.StudentID"), _out("ClassID", "dbo.Classes.ClassID"),
        ),
    )
    classes = _Doc(
        "dbo.Classes", column_names=("ClassID",), primary_key_columns=("ClassID",), has_primary_key=True,
        confirmed_relationship_sources=("dbo.Enrollments.ClassID",),
        confirmed_relationship_edges=(_in("ClassID", "dbo.Enrollments.ClassID"),),
    )
    view = _Doc(
        "dbo.vw_StudentClassSummary", object_type="VIEW",
        column_names=("StudentName", "ClassName", "EnrollmentDate", "Status"),
        synonyms=("student", "class"), business_name="Student Class Summary",
        description="Denormalized student/class summary",
        confirmed_relationship_edges=(
            _out("StudentName", "dbo.Students.StudentID"), _out("ClassName", "dbo.Classes.ClassID"),
            _out("ClassName", "dbo.Enrollments.ClassID"),
        ),
    )
    documents = [students, enrollments, classes, view]
    # Table candidates carry moderate (never maximal) structural confidence
    # -- an ordinary, uncertain-but-real match, not an artificially
    # decisive one either way. The view has NO candidacy at all here except
    # what view_authority itself proposes below.
    linking = _linking([
        _requirement("ENTITY:student", [("dbo.Students", 0.6)]),
        _requirement("DIMENSION:class", [("dbo.Classes", 0.6)]),
    ])

    view_result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(
            build_id=1, embedding_provider="test", embedding_model="test", embedding_dimensions=1,
        ),
    )
    assert any(c.object_fqn == "dbo.vw_StudentClassSummary" for c in view_result.candidates)

    augmented = subject.augment_linking_with_view_authority(linking, view_result)
    result = plan_schema_join_subgraph(augmented, documents)

    assert result.status == PLANNER_OK
    # Phase 6, Step 6H, Part 4 correction: a plan whose ONLY candidacy is
    # advisory (zero real mandatory-requirement coverage) is now correctly
    # Pareto-DOMINATED by the real multi-table plan (which is at least as
    # good on every dimension and strictly better on mandatory coverage,
    # structural quality, and relevance) and is removed before final
    # ranking rather than merely losing the ranking — a stronger, more
    # correct guarantee than this test's own pre-Step-6H version asked
    # for ("still offered, just never primary"). What actually matters —
    # authority alone can never WIN a mandatory requirement — still holds
    # unconditionally: the view is neither primary nor even offered as an
    # alternative once it provides literally zero real coverage.
    all_plans = (result.primary_plan, *result.alternative_plans)
    view_only_plans = [p for p in all_plans if p.selected_object_fqns == ("dbo.vw_StudentClassSummary",)]
    assert not view_only_plans, "a plan with zero real (non-advisory) mandatory coverage must be Pareto-dominated, never offered"
    assert result.primary_plan.selected_object_fqns != ("dbo.vw_StudentClassSummary",)
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Students", "dbo.Enrollments", "dbo.Classes"}


def test_view_with_genuine_structural_evidence_still_wins_when_authority_corroborates_it():
    """The corrected counterpart to the test above: when the view ALREADY
    has real, structural (non-advisory) candidacy for the mandatory
    concepts -- e.g. schema_linker's own dictionary/synonym matching
    already proposed it, exactly as it would for a real question -- view
    authority's role is legitimate: a bounded corroborating boost on TOP
    of real evidence, never the sole basis. A genuinely well-evidenced
    consolidating view can still legitimately win over a multi-table
    alternative through this module's own unmodified size/coverage
    preference, once it has real relevance to stand on."""
    students = _Doc(
        "dbo.Students", column_names=("StudentID",), primary_key_columns=("StudentID",), has_primary_key=True,
        confirmed_relationship_targets=("dbo.Enrollments.StudentID",),
        confirmed_relationship_edges=(_in("StudentID", "dbo.Enrollments.StudentID"),),
    )
    enrollments = _Doc(
        "dbo.Enrollments", column_names=("StudentID", "ClassID"),
        confirmed_relationship_sources=("dbo.Students.StudentID",), confirmed_relationship_targets=("dbo.Classes.ClassID",),
        confirmed_relationship_edges=(
            _out("StudentID", "dbo.Students.StudentID"), _out("ClassID", "dbo.Classes.ClassID"),
        ),
    )
    classes = _Doc(
        "dbo.Classes", column_names=("ClassID",), primary_key_columns=("ClassID",), has_primary_key=True,
        confirmed_relationship_sources=("dbo.Enrollments.ClassID",),
        confirmed_relationship_edges=(_in("ClassID", "dbo.Enrollments.ClassID"),),
    )
    view = _Doc(
        "dbo.vw_StudentClassSummary", object_type="VIEW",
        column_names=("StudentName", "ClassName", "EnrollmentDate", "Status"),
        synonyms=("student", "class"), business_name="Student Class Summary",
        description="Denormalized student/class summary",
        confirmed_relationship_edges=(
            _out("StudentName", "dbo.Students.StudentID"), _out("ClassName", "dbo.Classes.ClassID"),
            _out("ClassName", "dbo.Enrollments.ClassID"),
        ),
    )
    documents = [students, enrollments, classes, view]
    # The view now ALSO carries a real, structural (COLUMN_TOKEN) candidate
    # for both mandatory concepts, at GENUINELY STRONGER confidence than
    # either table -- exactly what schema_linker's own real dictionary/
    # synonym matching would have produced for a view documented this
    # thoroughly (an exact business-name/synonym match is real, direct
    # evidence, often stronger than an incidental column-name overlap).
    linking = _linking([
        _requirement("ENTITY:student", [("dbo.vw_StudentClassSummary", 0.7), ("dbo.Students", 0.6)]),
        _requirement("DIMENSION:class", [("dbo.vw_StudentClassSummary", 0.7), ("dbo.Classes", 0.6)]),
    ])

    # score_business_views deliberately never proposes a view already
    # present among a requirement's own candidates (it is not view_
    # authority's job to duplicate a requirement schema_linker's real
    # matching already satisfied) -- this test's point is authority
    # CORROBORATING (via score_business_views' other components: canonical
    # quality, business documentation, domain/source-role) an already-real
    # candidate, not introducing a new one, so augmentation may be a no-op
    # here and that is fine; what matters is the final planning outcome.
    view_result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(
            build_id=1, embedding_provider="test", embedding_model="test", embedding_dimensions=1,
        ),
    )

    augmented = subject.augment_linking_with_view_authority(linking, view_result)
    result = plan_schema_join_subgraph(augmented, documents)

    assert result.status == PLANNER_OK
    # Now genuinely evidenced (real structural candidacy for BOTH
    # requirements) -- the single view legitimately outranks the equally-
    # plausible multi-table alternative through the join planner's own
    # unmodified size/coverage preference, no authority boost required.
    assert result.primary_plan.selected_object_fqns == ("dbo.vw_StudentClassSummary",)


# ── 2. An incomplete view loses to a complete base-table plan ─────────────


def test_incomplete_view_never_outranks_a_complete_base_table_plan():
    students = _Doc(
        "dbo.Students", column_names=("StudentID", "ClassID"), primary_key_columns=("StudentID",), has_primary_key=True,
        confirmed_relationship_targets=("dbo.Classes.ClassID",),
        confirmed_relationship_edges=(_out("ClassID", "dbo.Classes.ClassID"),),
    )
    classes = _Doc(
        "dbo.Classes", column_names=("ClassID",), primary_key_columns=("ClassID",), has_primary_key=True,
        confirmed_relationship_sources=("dbo.Students.ClassID",),
        confirmed_relationship_edges=(_in("ClassID", "dbo.Students.ClassID"),),
    )
    # This view only covers ENTITY:student (its own tokens never overlap
    # "class" at all) -- it must never be offered as if it satisfied
    # DIMENSION:class too.
    partial_view = _Doc(
        "dbo.vw_StudentOnly", object_type="VIEW", column_names=("StudentName",), synonyms=("student",),
    )
    documents = [students, classes, partial_view]
    linking = _linking([
        _requirement("ENTITY:student", [("dbo.Students", 0.9)]),
        _requirement("DIMENSION:class", [("dbo.Classes", 0.9)]),
    ])

    view_result = subject.score_business_views(linking, documents)
    augmented = subject.augment_linking_with_view_authority(linking, view_result)
    result = plan_schema_join_subgraph(augmented, documents)

    assert result.status == PLANNER_OK
    # The complete two-table plan must win -- the partial view (which can
    # never alone cover DIMENSION:class) must never become the primary.
    assert set(result.primary_plan.selected_object_fqns) == {"dbo.Students", "dbo.Classes"}
    assert result.primary_plan.uncovered_requirement_keys == ()


# ── 3/7. An unrelated view never wins merely for being a view ─────────────


def test_unrelated_view_is_never_proposed_as_a_candidate():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    unrelated_view = _Doc(
        "dbo.vw_WeatherReport", object_type="VIEW", column_names=("Temperature", "Humidity"),
        synonyms=("temperature", "humidity", "forecast"),
    )
    documents = [students, unrelated_view]
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, documents)

    assert result.candidates == ()
    assert result.eligible_view_count == 1


# ── 4. Archive/history/backup/system views remain excluded ────────────────


def test_a_view_outside_the_supplied_eligible_document_set_is_never_considered():
    """Simulates Tier 3/4 exclusion: this module only ever sees whatever
    `documents` its caller (core.sqlservice.schema_plan_options, which
    already applies core.sqlservice.retrieval_documents.
    classify_retrieval_tier's existing tier policy) actually supplies —
    never re-implements or second-guesses that policy itself."""
    students = _Doc("dbo.Students", column_names=("StudentID",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    # "dbo.vw_Students_archive_2019" is deliberately never included in
    # `documents` -- exactly what happens to a real Tier 4 object.
    result = subject.score_business_views(linking, [students])

    assert result.candidates == ()
    assert result.eligible_view_count == 0


# ── 8. Ambiguous/competing views are both retained, bounded ───────────────


def test_multiple_competing_views_are_all_retained_up_to_the_bound():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    views = [
        _Doc(f"dbo.vw_Student{i}", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
        for i in range(8)
    ]
    documents = [students, *views]
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, documents)

    assert len(result.candidates) == subject.MAX_VIEW_CANDIDATES_PER_REQUIREMENT
    assert len({c.object_fqn for c in result.candidates}) == subject.MAX_VIEW_CANDIDATES_PER_REQUIREMENT


# ── 9. Deterministic ranking/tie-breaking ──────────────────────────────────


def test_ranking_is_deterministic_across_repeated_calls():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view_a = _Doc("dbo.vw_Zebra", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    view_b = _Doc("dbo.vw_Apple", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    documents = [students, view_a, view_b]
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    first = subject.score_business_views(linking, documents)
    second = subject.score_business_views(linking, documents)

    assert [c.object_fqn for c in first.candidates] == [c.object_fqn for c in second.candidates]
    # Identical scores -> canonical (lexicographic, lowercased) tie-break.
    assert [c.object_fqn for c in first.candidates] == ["dbo.vw_Apple", "dbo.vw_Zebra"]


# ── 10. Canonical FQN preservation ─────────────────────────────────────────


def test_canonical_fqn_case_is_preserved_in_candidates():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view = _Doc("dbo.Vw_MixedCase_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, [students, view])

    assert result.candidates[0].object_fqn == "dbo.Vw_MixedCase_Student"  # never lowercased


# ── 11. Aliases and case-insensitive matching (already-covered skip) ──────


def test_an_object_already_covered_case_insensitively_is_never_duplicated():
    view = _Doc("DBO.VW_STUDENTS", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    # The SAME object (different case) is already a structural candidate
    # for this requirement -- must never be proposed a second time.
    linking = _linking([_requirement("ENTITY:student", [("dbo.vw_Students", 0.9)])])

    result = subject.score_business_views(linking, [view])

    assert result.candidates == ()


# ── 12. Required-column coverage vs missing-column rejection ──────────────


def test_partial_token_overlap_is_reflected_in_the_coverage_component():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    # Only "student" overlaps the two-token requirement "student status".
    view = _Doc("dbo.vw_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student status", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, [students, view])

    assert len(result.candidates) == 1
    assert result.candidates[0].components.requirement_coverage == pytest.approx(0.5)


def test_zero_token_overlap_yields_no_candidate():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view = _Doc("dbo.vw_Unrelated", object_type="VIEW", column_names=("Foo", "Bar"), synonyms=("foo", "bar"))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, [students, view])

    assert result.candidates == ()


# ── 13. Confirmed view-dependency handling ─────────────────────────────────


def test_consolidation_count_reflects_real_confirmed_relationship_edges():
    view = _Doc(
        "dbo.vw_Wide", object_type="VIEW", column_names=("A",),
        confirmed_relationship_edges=(
            _out("A", "dbo.T1.Id"), _out("B", "dbo.T2.Id"), _in("C", "dbo.T3.Id"),
            _out("D", "dbo.T1.OtherId"),  # same object T1 again -- distinct OBJECTS, not edges
        ),
    )
    assert subject._consolidation_count(view) == 3  # dbo.T1, dbo.T2, dbo.T3


# ── 15. Cache isolation and invalidation ───────────────────────────────────


def test_view_token_index_cache_is_warm_on_second_call_with_same_identity():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view = _Doc("dbo.vw_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    subject.clear_view_authority_cache()
    subject.score_business_views(linking, [students, view], build_id=1, schema_fingerprint="fp-a")
    assert subject.view_authority_cache_size() == 1
    subject.score_business_views(linking, [students, view], build_id=1, schema_fingerprint="fp-a")
    assert subject.view_authority_cache_size() == 1  # still just one entry -- reused, not rebuilt


def test_view_token_index_cache_invalidates_for_a_new_build_or_fingerprint():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view = _Doc("dbo.vw_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    subject.clear_view_authority_cache()
    subject.score_business_views(linking, [students, view], build_id=1, schema_fingerprint="fp-a")
    subject.score_business_views(linking, [students, view], build_id=2, schema_fingerprint="fp-b")
    assert subject.view_authority_cache_size() == 2


# ── 16. Immutable results ───────────────────────────────────────────────────


def test_results_are_immutable():
    students = _Doc("dbo.Students", column_names=("StudentID",))
    view = _Doc("dbo.vw_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])

    result = subject.score_business_views(linking, [students, view])
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "CHANGED"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.candidates[0].score = 0.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.candidates[0].components.canonical_quality = 0.0


# ── 17. Bounded candidate count (already exercised by test 8 above; a
# second, explicit assertion on the module's own configured bound) ────────


def test_max_view_candidates_per_requirement_is_a_positive_bound():
    assert subject.MAX_VIEW_CANDIDATES_PER_REQUIREMENT >= 1
    assert subject.MAX_VIEWS_CONSIDERED >= subject.MAX_VIEW_CANDIDATES_PER_REQUIREMENT


# ── 18. No database-specific production constants ──────────────────────────────

_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "Enrollment", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "view_authority.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"view_authority.py contains banned domain-specific token {banned!r}"


# ── Component weights sanity (mirrors canonical_scoring's own test) ───────


def test_component_weights_are_bounded_and_sum_to_one():
    assert abs(sum(subject.COMPONENT_WEIGHTS.values()) - 1.0) < 1e-9
    for value in subject.COMPONENT_WEIGHTS.values():
        assert 0.0 < value <= 1.0


# ── augment_linking_with_view_authority: never touches unrelated requirements ──


def test_augment_never_changes_a_requirement_it_has_no_new_candidate_for():
    unrelated = _requirement("DIMENSION:class", [("dbo.Classes", 0.9)])
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)]), unrelated])
    view = _Doc("dbo.vw_Student", object_type="VIEW", column_names=("StudentName",), synonyms=("student",))

    view_result = subject.score_business_views(linking, [_Doc("dbo.Students", column_names=("StudentID",)), _Doc("dbo.Classes", column_names=("ClassID",)), view])
    augmented = subject.augment_linking_with_view_authority(linking, view_result)

    assert augmented.requirements.dimension_concepts[0] is unrelated  # byte-identical, untouched


def test_augment_returns_the_same_linking_object_when_nothing_to_add():
    linking = _linking([_requirement("ENTITY:student", [("dbo.Students", 0.9)])])
    empty_result = subject.ViewAuthorityResult(
        status=subject.STATUS_OK, candidates=(), considered_view_count=0, eligible_view_count=0,
        build_id=None, schema_fingerprint=None, warnings=(),
    )

    assert subject.augment_linking_with_view_authority(linking, empty_result) is linking
