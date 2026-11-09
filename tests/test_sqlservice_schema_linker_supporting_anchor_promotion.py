"""
Focused unit tests — Phase 6, Step 6I, Part 2: generic supporting-anchor
eligibility (core.sqlservice.schema_linker._qualifying_supporting_concept_
refs / the required_concept_refs expansion inside link_question_to_schema).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind.

Run:
    venv/Scripts/pytest tests/test_sqlservice_schema_linker_supporting_anchor_promotion.py -v
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from core.sqlservice.schema_linker import (
    CONCEPT_KIND_ENTITY,
    CONCEPT_KIND_IDENTIFIER,
    MATCH_KIND_COLUMN_TOKEN,
    MATCH_KIND_MODEL_SEMANTIC,
    MATCH_KIND_SYNONYM,
    MODEL_CANDIDATE_CONFIDENCE_CAP,
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    STRENGTH_SUPPORTING,
    STRENGTH_UNRESOLVED,
    MatchedRequirement,
    RequirementCandidate,
    _ENTITY_SINGLE_TOKEN_CORE_FLOOR,
    _qualifying_supporting_concept_refs,
    clear_schema_linker_cache,
    link_question_to_schema,
)
from core.sqlservice.semantic_catalog import build_semantic_catalog

# ── Fixture builder (mirrors tests/test_sqlservice_schema_linker.py) ──────

_DEFAULT_META = {
    "business_name": None, "description": None, "domain": None, "synonyms": (),
    "column_names": (), "column_descriptors": (), "has_primary_key": False,
    "primary_key_columns": (), "foreign_key_targets": (), "confirmed_relationship_targets": (),
    "confirmed_relationship_sources": (), "confirmed_relationship_edges": (),
    "inferred_relationship_targets": (), "inferred_relationship_sources": (),
    "status_columns": (), "date_columns": (), "measure_columns": (),
}


class _Doc:
    def __init__(self, object_fqn: str, object_type: str = "TABLE", retrieval_tier: int = 1, **meta_overrides):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = retrieval_tier
        merged = dict(_DEFAULT_META)
        merged.update(meta_overrides)
        self.metadata = MappingProxyType(merged)


@pytest.fixture(autouse=True)
def _isolated_cache():
    clear_schema_linker_cache()
    yield
    clear_schema_linker_cache()


def _cand(
    object_fqn: str, *, confidence: float, informativeness: float = 1.0,
    match_kind: str = MATCH_KIND_SYNONYM, column_name: str | None = None,
) -> RequirementCandidate:
    return RequirementCandidate(
        object_fqn=object_fqn, column_name=column_name, technical_label="x", match_kind=match_kind,
        confidence=confidence, raw_confidence=confidence, informativeness=informativeness, evidence=(),
    )


def _req(concept_kind: str, label: str, strength: str, candidates: tuple[RequirementCandidate, ...]) -> MatchedRequirement:
    return MatchedRequirement(
        concept_kind=concept_kind, concept_key=f"{concept_kind}:{label}", normalized_label=label,
        matched_terms=(label,), candidates=candidates, candidate_total_count=len(candidates),
        confidence=candidates[0].confidence if candidates else 0.0, ambiguous=False, evidence=(),
        strength=strength,
    )


_STRONG_CONF = MODEL_CANDIDATE_CONFIDENCE_CAP + 0.1
_STRONG_INFO = _ENTITY_SINGLE_TOKEN_CORE_FLOOR + 0.1
_WEAK_INFO = _ENTITY_SINGLE_TOKEN_CORE_FLOOR - 0.1
_WEAK_CONF = MODEL_CANDIDATE_CONFIDENCE_CAP - 0.1


# ── Direct unit tests against _qualifying_supporting_concept_refs ─────────


def test_strong_direct_supporting_candidate_is_promoted():
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "bridge_id", STRENGTH_SUPPORTING,
        (_cand("dbo.Bridge", confidence=_STRONG_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.bridge"}), frozenset({"dbo.bridge"}),
    )
    assert promoted == frozenset({"IDENTIFIER:bridge_id"})


def test_generic_low_informativeness_supporting_candidate_is_not_promoted():
    """A strong-confidence match on a widely-shared/generic token (low
    informativeness) must never become an anchor -- 'generic shared words
    must not create anchors'."""
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "widely_shared_id", STRENGTH_SUPPORTING,
        (_cand("dbo.Generic", confidence=_STRONG_CONF, informativeness=_WEAK_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.generic"}), frozenset({"dbo.generic"}),
    )
    assert promoted == frozenset()


def test_weak_confidence_supporting_candidate_is_not_promoted():
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "weak_id", STRENGTH_SUPPORTING,
        (_cand("dbo.Weak", confidence=_WEAK_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.weak"}), frozenset({"dbo.weak"}),
    )
    assert promoted == frozenset()


def test_model_only_supporting_evidence_is_never_promoted():
    """Even at the maximum possible model-inferred confidence and full
    informativeness, MATCH_KIND_MODEL_SEMANTIC evidence alone can never
    create an anchor -- Part 2's 'model-generated aliases alone cannot
    create anchor eligibility'."""
    requirement = _req(
        CONCEPT_KIND_ENTITY, "model_only", STRENGTH_SUPPORTING,
        (_cand(
            "dbo.ModelOnly", confidence=MODEL_CANDIDATE_CONFIDENCE_CAP, informativeness=1.0,
            match_kind=MATCH_KIND_MODEL_SEMANTIC,
        ),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.modelonly"}), frozenset({"dbo.modelonly"}),
    )
    assert promoted == frozenset()


def test_candidate_outside_admitted_pool_is_not_promoted():
    """Eligibility 'the candidate is already present in the bounded
    relevance pool' -- a strong/informative candidate that never cleared
    relevance admission must not be promoted."""
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "unadmitted_id", STRENGTH_SUPPORTING,
        (_cand("dbo.NotAdmitted", confidence=_STRONG_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset(), frozenset({"dbo.notadmitted"}),
    )
    assert promoted == frozenset()


def test_candidate_outside_eligible_tier_is_not_promoted():
    """Defense in depth: a candidate absent from the eligible (Tier 1/2)
    set is never promoted, even if (adversarially) marked admitted."""
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "ineligible_id", STRENGTH_SUPPORTING,
        (_cand("dbo.Ineligible", confidence=_STRONG_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.ineligible"}), frozenset(),
    )
    assert promoted == frozenset()


def test_core_and_constraint_requirements_are_never_touched():
    """Part 2 is scoped to SUPPORTING only -- CORE/CONSTRAINT requirements
    (already anchor-mandatory) must never appear in the promoted set,
    regardless of their own candidates' evidence."""
    core_req = _req(
        CONCEPT_KIND_ENTITY, "already_core", STRENGTH_CORE,
        (_cand("dbo.AlreadyCore", confidence=_STRONG_CONF, informativeness=_STRONG_INFO),),
    )
    constraint_req = _req(
        CONCEPT_KIND_ENTITY, "already_constraint", STRENGTH_CONSTRAINT,
        (_cand("dbo.AlreadyConstraint", confidence=_STRONG_CONF, informativeness=_STRONG_INFO),),
    )
    unresolved_req = _req(
        CONCEPT_KIND_ENTITY, "unresolved", STRENGTH_UNRESOLVED,
        (_cand("dbo.Unresolved", confidence=_STRONG_CONF, informativeness=_STRONG_INFO),),
    )
    promoted = _qualifying_supporting_concept_refs(
        (core_req, constraint_req, unresolved_req),
        frozenset({"dbo.alreadycore", "dbo.alreadyconstraint", "dbo.unresolved"}),
        frozenset({"dbo.alreadycore", "dbo.alreadyconstraint", "dbo.unresolved"}),
    )
    assert promoted == frozenset()


def test_one_weak_candidate_does_not_block_a_different_strong_candidate_on_the_same_requirement():
    requirement = _req(
        CONCEPT_KIND_IDENTIFIER, "mixed_id", STRENGTH_SUPPORTING,
        (
            _cand("dbo.Weak", confidence=_WEAK_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),
            _cand("dbo.Strong", confidence=_STRONG_CONF, informativeness=_STRONG_INFO, match_kind=MATCH_KIND_COLUMN_TOKEN),
        ),
    )
    promoted = _qualifying_supporting_concept_refs(
        (requirement,), frozenset({"dbo.weak", "dbo.strong"}), frozenset({"dbo.weak", "dbo.strong"}),
    )
    assert promoted == frozenset({"IDENTIFIER:mixed_id"})


# ── Integration-level: the promotion actually changes anchor selection ────


def _bridge_schema():
    """A bridge/junction table (Signups) reachable ONLY via a generic-
    looking but, in THIS build, genuinely specific identifier column
    ('EventID') shared with exactly one other table -- plus a flood of
    unrelated tables whose own stronger overall relevance would otherwise
    crowd it out of the small non-concept-fill pool."""
    event = _Doc(
        "dbo.Events", business_name="Event", description="Scheduled events",
        column_names=("EventID", "EventName"), has_primary_key=True, primary_key_columns=("EventID",),
        column_descriptors=(
            {"column_name": "EventID", "normalized_sql_type": "INTEGER", "is_primary_key": True, "is_foreign_key": False},
        ),
    )
    signups = _Doc(
        "dbo.Signups", column_names=("SignupID", "EventID", "PersonID"),
        has_primary_key=True, primary_key_columns=("SignupID",),
        foreign_key_targets=("dbo.Events.EventID",),
        confirmed_relationship_targets=("dbo.Events.EventID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "EventID", "other": "dbo.Events.EventID", "kind": "PHYSICAL_FK"},
        ),
        column_descriptors=(
            {"column_name": "SignupID", "normalized_sql_type": "INTEGER", "is_primary_key": True, "is_foreign_key": False},
            {"column_name": "EventID", "normalized_sql_type": "INTEGER", "is_primary_key": False, "is_foreign_key": True},
        ),
    )
    unrelated = [
        _Doc(
            f"dbo.Unrelated{i}", business_name=f"Unrelated{i}", description="an unrelated broad table",
            column_names=("ID", "Description"),
        )
        for i in range(12)
    ]
    return [event, signups, *unrelated]


def test_strong_supporting_identifier_candidate_becomes_an_anchor_end_to_end():
    docs = _bridge_schema()
    catalog = build_semantic_catalog(docs)
    result = link_question_to_schema("Show events and their EventID signups", catalog, docs)

    signup_requirement = next(
        (r for r in result.requirements.identifier_concepts if any(c.object_fqn == "dbo.Signups" for c in r.candidates)),
        None,
    )
    assert signup_requirement is not None, "expected an IDENTIFIER requirement with a dbo.Signups candidate"
    assert signup_requirement.strength == STRENGTH_SUPPORTING

    anchor_fqns = {a.object_fqn for a in result.anchors}
    assert "dbo.Signups" in anchor_fqns
    assert result.supporting_anchor_promotions_count >= 1
