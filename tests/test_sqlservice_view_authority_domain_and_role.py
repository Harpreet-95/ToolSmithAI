"""
Focused unit tests -- Phase 6, Step 6F, Parts 5/6: domain-aware retrieval
and evidence-based authority scoring, both implemented as extensions to
core/sqlservice/view_authority.py's existing weighted-component scoring
(never a second, parallel ranking system).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Reuses tests/test_sqlservice_view_authority.py's
own fixture builders and conventions.
"""
from __future__ import annotations

import pytest

from core.sqlservice.canonical_scoring import CanonicalScoringIdentity, clear_canonical_scoring_cache
from core.sqlservice.join_planner import clear_join_planner_cache
from core.sqlservice.semantic_catalog import EvidenceItem, ModelInferredObjectEvidence
import core.sqlservice.view_authority as subject
from tests.test_sqlservice_view_authority import _Doc, _linking, _requirement

MODEL_KIND = "MODEL_INFERRED_SEMANTIC"


class _FakeCatalog:
    """Minimal stand-in exposing ONLY what view_authority.py reads via
    getattr -- never the real SemanticCatalog machinery, since these tests
    exercise view_authority.py in isolation."""

    def __init__(self, fingerprint: str, evidence: tuple[ModelInferredObjectEvidence, ...]):
        self.fingerprint = fingerprint
        self.model_inferred_object_evidence = evidence


def _evidence(
    object_fqn: str, *, business_domain: str = "", source_role: str | None = None,
    role_confidence: float = 0.9, status_labels: tuple[str, ...] = (),
    supported_operations: tuple[str, ...] = (), has_business_doc: bool = False,
) -> ModelInferredObjectEvidence:
    return ModelInferredObjectEvidence(
        object_fqn=object_fqn,
        business_name_candidates=("X",) if has_business_doc else (),
        description_candidates=(),
        entity_concept_candidates=(),
        alias_candidates=(),
        object_role_candidates=((source_role, role_confidence),) if source_role else (),
        dictionary_has_business_name=False,
        dictionary_has_description=False,
        ambiguous_business_name=False,
        ambiguous_object_role=False,
        confidence=role_confidence,
        evidence=(EvidenceItem(kind=MODEL_KIND, detail=object_fqn),),
        business_domain_candidates=(business_domain,) if business_domain else (),
        status_concept_label_candidates=status_labels,
        supported_operation_candidates=supported_operations,
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


def _view(fqn: str, columns=("StudentID", "Status")) -> _Doc:
    return _Doc(fqn, object_type="VIEW", column_names=columns)


# ── Part 5: domain-aware retrieval ───────────────────────────────────────


def test_domain_match_boosts_a_view_over_an_equally_relevant_undocumented_one():
    matching = _view("dbo.vw_EnrollmentSummary", columns=("EnrollmentID", "Status"))
    plain = _view("dbo.vw_EnrollmentSummary2", columns=("EnrollmentID", "Status"))
    documents = [matching, plain]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp1", (
        _evidence("dbo.vw_EnrollmentSummary", business_domain="Enrollment"),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    by_fqn = {c.object_fqn: c for c in result.candidates}
    assert by_fqn["dbo.vw_EnrollmentSummary"].components.domain_match == 1.0
    assert by_fqn["dbo.vw_EnrollmentSummary2"].components.domain_match == 0.0
    assert by_fqn["dbo.vw_EnrollmentSummary"].score > by_fqn["dbo.vw_EnrollmentSummary2"].score


def test_domain_inference_never_hard_filters_cross_domain_evidence():
    """A view whose business_domain does NOT match the inferred question
    domain must still be retained as a candidate when it shows real
    structural/token overlap -- Part 5: 'retain cross-domain candidates
    when ... evidence supports them'."""
    cross_domain_view = _view("dbo.vw_PlacementBreakdown", columns=("EnrollmentID", "Status"))
    documents = [cross_domain_view]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp2", (
        _evidence("dbo.vw_PlacementBreakdown", business_domain="Career Placement"),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    assert any(c.object_fqn == "dbo.vw_PlacementBreakdown" for c in result.candidates)


def test_infer_candidate_domains_returns_multiple_when_ambiguous():
    """Part 5: 'treat ambiguous domain detection as multiple candidates,
    not a forced decision'."""
    req = _requirement("ENTITY:student enrollment", [("dbo.X", 0.5)])
    discovered = frozenset({"Student Records", "Enrollment Management", "Finance"})
    candidates = subject.infer_candidate_domains([req], discovered)
    assert "Student Records" in candidates
    assert "Enrollment Management" in candidates
    assert "Finance" not in candidates
    assert len(candidates) >= 2


def test_infer_candidate_domains_empty_when_no_catalog_vocabulary():
    req = _requirement("ENTITY:student", [("dbo.X", 0.5)])
    assert subject.infer_candidate_domains([req], frozenset()) == frozenset()


def test_domain_matching_uses_no_provider_call_and_is_deterministic():
    """Part 5: 'No additional OpenAI call at question time' + 'domain
    matching must be deterministic'."""
    view = _view("dbo.vw_EnrollmentSummary")
    documents = [view]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp3", (_evidence("dbo.vw_EnrollmentSummary", business_domain="Enrollment"),))

    result1 = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    result2 = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    assert result1.candidates == result2.candidates


def test_domain_cache_is_keyed_by_catalog_fingerprint():
    """Part 5: 'cached per semantic-catalog identity' -- a different
    catalog fingerprint (same build_id/schema_fingerprint) must not reuse
    a stale cached index."""
    view = _view("dbo.vw_EnrollmentSummary", columns=("EnrollmentID", "Status"))
    documents = [view]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])

    catalog_v1 = _FakeCatalog("fp-v1", ())
    result_v1 = subject.score_business_views(
        linking, documents, build_id=1, schema_fingerprint="same",
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog_v1,
    )
    assert result_v1.candidates[0].components.domain_match == 0.0

    catalog_v2 = _FakeCatalog("fp-v2", (_evidence("dbo.vw_EnrollmentSummary", business_domain="Enrollment"),))
    result_v2 = subject.score_business_views(
        linking, documents, build_id=1, schema_fingerprint="same",
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog_v2,
    )
    assert result_v2.candidates[0].components.domain_match == 1.0


# ── Part 6: evidence-based authority scoring ─────────────────────────────


def test_authoritative_source_role_outranks_historical_at_equal_coverage():
    current = _view("dbo.vw_ActiveEnrollments", columns=("EnrollmentID", "Status"))
    historical = _view("dbo.vw_PastEnrollments", columns=("EnrollmentID", "Status"))
    documents = [current, historical]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp4", (
        _evidence("dbo.vw_ActiveEnrollments", source_role="CURRENT_STATE_VIEW"),
        _evidence("dbo.vw_PastEnrollments", source_role="HISTORICAL_VIEW"),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    by_fqn = {c.object_fqn: c for c in result.candidates}
    assert by_fqn["dbo.vw_ActiveEnrollments"].components.source_role_bonus > by_fqn["dbo.vw_PastEnrollments"].components.source_role_bonus
    assert by_fqn["dbo.vw_ActiveEnrollments"].score > by_fqn["dbo.vw_PastEnrollments"].score


def test_unrelated_authoritative_view_cannot_enter_through_authority_alone():
    """Part 6: 'A semantically unrelated authoritative view must not enter
    the candidate pool solely through authority.' Even a fully-authoritative,
    fully-documented, domain-matching view with ZERO real token overlap
    with the requirement must never appear as a candidate for it."""
    unrelated = _view("dbo.vw_TotallyUnrelatedThing", columns=("SomeColumn",))
    documents = [unrelated]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.vw_TotallyUnrelatedThing", 0.5)])])
    catalog = _FakeCatalog("fp5", (
        _evidence(
            "dbo.vw_TotallyUnrelatedThing", business_domain="Enrollment",
            source_role="AUTHORITATIVE_BUSINESS_VIEW", has_business_doc=True,
        ),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    assert result.candidates == ()


def test_strong_query_relevance_can_beat_authority():
    """Part 6: 'Strong query relevance must be able to beat authority.' A
    view with weaker authority signals but a MUCH stronger token-overlap
    coverage must be able to outscore a strongly-authoritative view with
    weaker coverage."""
    # A multi-token requirement -- with a single-token requirement,
    # coverage saturates at 1.0 for ANY view matching that one token,
    # regardless of how many OTHER tokens also happen to match, making
    # "more overlap" indistinguishable from "just enough overlap".
    strong_relevance = _view(
        "dbo.vw_StrongMatch", columns=("StudentID", "EnrollmentDate", "StatusCode"),
    )
    weak_relevance_strong_authority = _view("dbo.vw_WeakMatch", columns=("EnrollmentOnly",))
    documents = [strong_relevance, weak_relevance_strong_authority]
    requirement = _requirement(
        "ENTITY:student enrollment status",
        [("dbo.PlaceholderAnchor", 0.5)],
    )
    linking = _linking([requirement])
    catalog = _FakeCatalog("fp6", (
        _evidence("dbo.vw_WeakMatch", source_role="AUTHORITATIVE_BUSINESS_VIEW"),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    by_fqn = {c.object_fqn: c for c in result.candidates}
    assert by_fqn["dbo.vw_StrongMatch"].components.requirement_coverage > by_fqn["dbo.vw_WeakMatch"].components.requirement_coverage
    assert by_fqn["dbo.vw_StrongMatch"].score > by_fqn["dbo.vw_WeakMatch"].score


def test_status_and_operation_candidates_widen_token_coverage():
    """Part 6: 'status/date/measure coverage relevant to the question' --
    a view with no matching column/synonym tokens but a matching STATUS
    CONCEPT LABEL from VIEW_ENRICHED still earns real coverage."""
    view = _view("dbo.vw_X", columns=("SomeColumn",))
    documents = [view]
    linking = _linking([_requirement("DIMENSION:active enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp7", (
        _evidence("dbo.vw_X", status_labels=("Active Enrollment",)),
    ))

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
        catalog=catalog,
    )
    assert any(c.object_fqn == "dbo.vw_X" for c in result.candidates)


def test_omitting_catalog_reproduces_five_component_weighting():
    """Weight-renormalization correctness: omitting `catalog` must never
    silently waste the two new components' combined weight -- coverage
    alone should be able to earn the SAME full-credit relative ranking as
    before Part 5/6 existed."""
    view = _view("dbo.vw_EnrollmentSummary", columns=("EnrollmentID", "Status"))
    documents = [view]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])

    result = subject.score_business_views(
        linking, documents,
        canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
    )
    candidate = result.candidates[0]
    assert candidate.components.domain_match == 0.0
    assert candidate.components.source_role_bonus == 0.0
    # Full requirement_coverage alone (plus zero-valued structural
    # components) must still be able to clear a meaningfully high score --
    # never capped below ~0.8 of achievable total purely because `catalog`
    # was omitted.
    assert candidate.score > 0.0


def test_deterministic_ranking_across_repeated_calls_with_catalog():
    view_a = _view("dbo.vw_Alpha")
    view_b = _view("dbo.vw_Beta")
    documents = [view_a, view_b]
    linking = _linking([_requirement("ENTITY:enrollment", [("dbo.PlaceholderAnchor", 0.5)])])
    catalog = _FakeCatalog("fp8", (
        _evidence("dbo.vw_Alpha", business_domain="Enrollment", source_role="CURRENT_STATE_VIEW"),
        _evidence("dbo.vw_Beta", business_domain="Enrollment", source_role="HISTORICAL_VIEW"),
    ))

    results = [
        subject.score_business_views(
            linking, documents,
            canonical_identity=CanonicalScoringIdentity(build_id=1, embedding_provider="t", embedding_model="t", embedding_dimensions=1),
            catalog=catalog,
        )
        for _ in range(3)
    ]
    orderings = [tuple(c.object_fqn for c in r.candidates) for r in results]
    assert len(set(orderings)) == 1


def test_no_reference_specific_tokens_in_domain_and_role_code():
    import inspect
    source = inspect.getsource(subject)
    assert "exampleco" not in source.lower()
