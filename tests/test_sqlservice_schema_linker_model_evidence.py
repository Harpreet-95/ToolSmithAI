"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4B1: validated
model-inferred semantics as advisory candidate evidence in
core.sqlservice.schema_linker (Parts 1-7 + the synthetic architecture
test).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Reuses the exact `_Doc`/`_RankedHit`/
`_RetrievedExample`/`_link`/`_anchor` fixture conventions already
established in tests/test_sqlservice_schema_linker.py, plus the
`_FakeEnrichment`/`_FakeColEnrichment`/`_FakeEvidence` duck-typed
StoredSemanticEnrichment stand-ins already established in
tests/test_sqlservice_semantic_catalog_enrichment_merge.py (both
duplicated here per this codebase's own "small test fixtures live per
module" convention).

Run:
    venv/Scripts/pytest tests/test_sqlservice_schema_linker_model_evidence.py -v
"""

from __future__ import annotations

import dataclasses
import inspect
import random
from types import MappingProxyType

import pytest

from core.sqlservice.join_planner import (
    clear_join_planner_cache,
    join_planner_cache_size,
    plan_schema_join_subgraph,
    prepare_join_graph_index,
)
from core.sqlservice.semantic_catalog import MODEL_INFERRED_PROVENANCE_KIND, build_semantic_catalog
from core.sqlservice.schema_linker import (
    ADMISSION_REASON_MODEL_MATCH,
    ANCHOR_WEIGHT_MODEL_SEMANTIC,
    MATCH_KIND_MODEL_SEMANTIC,
    MODEL_CANDIDATE_CONFIDENCE_CAP,
    AnchorCandidate,
    RequirementCandidate,
    STRENGTH_CONSTRAINT,
    STRENGTH_CORE,
    clear_schema_linker_cache,
    link_question_to_schema,
    prepare_linker_index,
    schema_linker_cache_size,
)

# ── Fixture builders (mirrors tests/test_sqlservice_schema_linker.py) ─────

_DEFAULT_META = {
    "business_name": None,
    "description": None,
    "domain": None,
    "synonyms": (),
    "column_names": (),
    "column_descriptors": (),
    "has_primary_key": False,
    "primary_key_columns": (),
    "foreign_key_targets": (),
    "confirmed_relationship_targets": (),
    "confirmed_relationship_sources": (),
    "confirmed_relationship_edges": (),
    "inferred_relationship_targets": (),
    "inferred_relationship_sources": (),
    "status_columns": (),
    "date_columns": (),
    "measure_columns": (),
}


class _Doc:
    def __init__(self, object_fqn: str, object_type: str = "TABLE", retrieval_tier: int = 1, **meta_overrides):
        self.object_fqn = object_fqn
        self.object_type = object_type
        self.retrieval_tier = retrieval_tier
        merged = dict(_DEFAULT_META)
        merged.update(meta_overrides)
        self.metadata = MappingProxyType(merged)


class _RankedHit:
    def __init__(self, object_fqn: str, score: float):
        self.object_fqn = object_fqn
        self.score = score


@pytest.fixture(autouse=True)
def _isolated_caches():
    clear_schema_linker_cache()
    clear_join_planner_cache()
    yield
    clear_schema_linker_cache()
    clear_join_planner_cache()


def _link(question, docs, catalog, **kwargs):
    return link_question_to_schema(question, catalog, docs, **kwargs)


def _anchor(result, object_fqn):
    return next((a for a in result.anchors if a.object_fqn == object_fqn), None)


def _core_constraint_keys(requirements) -> set[str]:
    all_reqs = (
        requirements.entity_concepts + requirements.measure_concepts + requirements.dimension_concepts
        + requirements.date_role_concepts + requirements.identifier_concepts
    )
    return {r.concept_key for r in all_reqs if r.strength in (STRENGTH_CORE, STRENGTH_CONSTRAINT)}


# ── Model-evidence duck-typed fixtures (mirrors
#    tests/test_sqlservice_semantic_catalog_enrichment_merge.py) ──────────


class _FakeEvidence:
    def __init__(self, kind, detail):
        self.kind = kind
        self.detail = detail


_MODEL_PROVENANCE = (_FakeEvidence(MODEL_INFERRED_PROVENANCE_KIND, "x"),)


class _FakeColEnrichment:
    def __init__(
        self, column_name, *, confidence=0.4, business_label="", semantic_role=None,
        date_role=None, measure_role=None, dimension_role=None, identifier_role=None,
        provenance=_MODEL_PROVENANCE,
    ):
        self.column_name = column_name
        self.business_label = business_label
        self.semantic_role = semantic_role
        self.date_role = date_role
        self.measure_role = measure_role
        self.dimension_role = dimension_role
        self.identifier_role = identifier_role
        self.confidence = confidence
        self.provenance = provenance


class _FakeEnrichment:
    def __init__(
        self, object_fqn, *, business_name=None, description=None, object_role="ENTITY",
        entity_concepts=(), aliases=(), supported_operations=(), confidence=0.4,
        columns=(), provenance=_MODEL_PROVENANCE,
    ):
        self.object_fqn = object_fqn
        self.business_name = business_name
        self.description = description
        self.object_role = object_role
        self.entity_concepts = entity_concepts
        self.aliases = aliases
        self.supported_operations = supported_operations
        self.confidence = confidence
        self.columns = columns
        self.provenance = provenance


def _col_descriptor(name, sql_type, *, pk=False, fk=False):
    return {"column_name": name, "normalized_sql_type": sql_type, "is_primary_key": pk, "is_foreign_key": fk}


# ── Part 1: immutable question intent ──────────────────────────────────────


def test_requirement_fingerprint_identical_with_and_without_enrichment():
    doc = _Doc(
        "dbo.Stu", business_name="Student", column_names=("StuID",),
        primary_key_columns=("StuID",), has_primary_key=True,
        column_descriptors=(_col_descriptor("StuID", "INTEGER", pk=True),),
    )
    baseline_catalog = build_semantic_catalog([doc])
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Learner", aliases=("scholar",), confidence=0.4),)
    enriched_catalog = build_semantic_catalog([doc], enrichment_input=enrichment)

    without = _link("How many students are there", [doc], baseline_catalog)
    with_ = _link("How many students are there", [doc], enriched_catalog)

    assert without.requirement_fingerprint == with_.requirement_fingerprint
    assert without.requirements.operation == with_.requirements.operation
    assert without.requirements.wants_comparison == with_.requirements.wants_comparison
    assert without.requirements.wants_trend == with_.requirements.wants_trend
    assert without.requirements.wants_grouping == with_.requirements.wants_grouping


def test_same_core_and_constraint_requirement_keys_with_and_without_enrichment():
    doc = _Doc(
        "dbo.Stu", business_name="Student", column_names=("StuID",),
        primary_key_columns=("StuID",), has_primary_key=True,
        column_descriptors=(_col_descriptor("StuID", "INTEGER", pk=True),),
    )
    baseline_catalog = build_semantic_catalog([doc])
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Learner", object_role="EVENT", confidence=0.4),)
    enriched_catalog = build_semantic_catalog([doc], enrichment_input=enrichment)

    without = _link("How many students are there", [doc], baseline_catalog)
    with_ = _link("How many students are there", [doc], enriched_catalog)

    assert _core_constraint_keys(without.requirements) == _core_constraint_keys(with_.requirements)


def test_model_evidence_does_not_change_detected_operation():
    doc = _Doc("dbo.Stu", business_name="Student", column_names=("StuID",))
    without_catalog = build_semantic_catalog([doc])
    enriched_catalog = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.Stu", object_role="EVENT", confidence=0.4),),
    )
    r1 = _link("How many students are there", [doc], without_catalog)
    r2 = _link("How many students are there", [doc], enriched_catalog)
    assert r1.requirements.operation == r2.requirements.operation


def test_model_evidence_alone_never_creates_a_new_requirement():
    # "dbo.Stu" has NO business_name at all -- its bare object name yields
    # head noun "stu", never "student". Model evidence proposing
    # business_name="Student" for it must therefore contribute NOTHING: no
    # entity concept named "student" exists anywhere in the real catalog for
    # it to attach to.
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enriched_catalog = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.Stu", business_name="Student", confidence=0.4),),
    )
    result = _link("How many students are there", [doc], enriched_catalog)
    assert not any(r.normalized_label == "student" for r in result.requirements.entity_concepts)


# ── Part 2: advisory object-candidate index ────────────────────────────────


def test_model_alias_adds_candidate_to_existing_entity_requirement():
    weak = _Doc("dbo.StuWeak", business_name="Student", column_names=("ID",))
    opaque = _Doc("dbo.T7X92", column_names=("ID",))
    docs = [weak, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T7X92", business_name="Student", confidence=0.4),),
    )
    result = _link("How many students are there", docs, catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")
    assert {c.object_fqn for c in entity_req.candidates} == {"dbo.StuWeak", "dbo.T7X92"}
    model_candidate = next(c for c in entity_req.candidates if c.object_fqn == "dbo.T7X92")
    assert model_candidate.match_kind == MATCH_KIND_MODEL_SEMANTIC
    assert model_candidate.confidence <= MODEL_CANDIDATE_CONFIDENCE_CAP


def test_model_business_name_maps_a_technical_object_to_an_explicit_question_concept():
    weak = _Doc("dbo.StuWeak", business_name="Student", column_names=("ID",))
    technical = _Doc("dbo.ZQ88TBL", column_names=("ID",))  # a fully opaque, technical-looking name
    docs = [weak, technical]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.ZQ88TBL", business_name="Student", confidence=0.4),),
    )
    result = _link("How many students are there", docs, catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")
    assert "dbo.ZQ88TBL" in {c.object_fqn for c in entity_req.candidates}


def test_model_alias_confidence_discounted_when_the_matched_word_is_a_generic_shared_alias():
    """Phase 6, Step 6G, Part 3: "generic concepts shared by many objects
    must have reduced discriminative value" applies to model-inferred
    candidates exactly like it already applies to real structural entity-
    alias matches (core.sqlservice.schema_linker's own entity_alias_
    token_normalized_idf, reused here rather than a second/duplicate
    computation) -- a single-token match on a word many DISTINCT real
    entities already use as an incidental alias fragment is generic
    regardless of who claims it."""
    group_entity = _Doc("dbo.Group", business_name="Group", column_names=("ID",))
    opaque = _Doc("dbo.T1", column_names=("ID",))

    # Scenario A: "group" is not shared as an alias by any OTHER entity --
    # full, undiscounted model confidence.
    docs_a = [group_entity, opaque]
    catalog_a = build_semantic_catalog(
        docs_a, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Group", confidence=0.5),),
    )
    result_a = _link("How many groups are there", docs_a, catalog_a)
    entity_req_a = next(r for r in result_a.requirements.entity_concepts if r.normalized_label == "group")
    model_candidate_a = next(c for c in entity_req_a.candidates if c.object_fqn == "dbo.T1")

    # Scenario B: two OTHER real entities also alias "group" -- the token
    # is now genuinely generic (document frequency 2 for real entity-alias
    # matching), so the SAME model claim must be discounted.
    team_entity = _Doc("dbo.TeamThing", business_name="Team", synonyms=("group",), column_names=("ID",))
    cluster_entity = _Doc("dbo.ClusterThing", business_name="Cluster", synonyms=("group",), column_names=("ID",))
    docs_b = [group_entity, team_entity, cluster_entity, opaque]
    catalog_b = build_semantic_catalog(
        docs_b, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Group", confidence=0.5),),
    )
    result_b = _link("How many groups are there", docs_b, catalog_b)
    entity_req_b = next(r for r in result_b.requirements.entity_concepts if r.normalized_label == "group")
    model_candidate_b = next(c for c in entity_req_b.candidates if c.object_fqn == "dbo.T1")

    assert model_candidate_a.informativeness == 1.0
    assert model_candidate_b.informativeness < 1.0
    assert model_candidate_b.confidence < model_candidate_a.confidence


def test_unknown_object_evidence_is_never_indexed():
    doc = _Doc("dbo.Stu", business_name="Student", column_names=("ID",))
    catalog = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.DoesNotExist", business_name="Student", confidence=0.4),),
    )
    assert catalog.model_inferred_object_evidence == ()  # rejected at the semantic_catalog layer already
    result = _link("How many students are there", [doc], catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")
    assert {c.object_fqn for c in entity_req.candidates} == {"dbo.Stu"}


def test_dictionary_candidate_outranks_model_only_candidate_regardless_of_raw_confidence():
    weak_structural = _Doc("dbo.Weak", retrieval_tier=2, column_names=("ID",))  # bare name -> low structural confidence
    opaque = _Doc("dbo.T1", column_names=("ID",))
    docs = [weak_structural, opaque]
    # Deliberately claim the maximum possible model confidence (0.50) --
    # still must never outrank the structural candidate, however low ITS
    # own confidence is.
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Weak", confidence=0.99),),
    )
    result = _link("How many weak items are there", docs, catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "weak")
    assert entity_req.candidates[0].object_fqn == "dbo.Weak"
    assert entity_req.candidates[0].match_kind != MATCH_KIND_MODEL_SEMANTIC
    assert entity_req.confidence == entity_req.candidates[0].confidence  # unaffected by the model candidate


def test_model_confidence_remains_capped_even_if_upstream_claims_higher():
    weak = _Doc("dbo.StuWeak", business_name="Student", column_names=("ID",))
    opaque = _Doc("dbo.T1", column_names=("ID",))
    docs = [weak, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Student", confidence=0.99),),
    )
    result = _link("How many students are there", docs, catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")
    model_candidate = next(c for c in entity_req.candidates if c.object_fqn == "dbo.T1")
    assert model_candidate.confidence <= MODEL_CANDIDATE_CONFIDENCE_CAP


# ── Part 4: conflict and ambiguity ──────────────────────────────────────────


def test_conflicting_model_candidates_marked_model_ambiguous_not_real_ambiguous():
    # Structurally DISTINCT opaque tables (different names/columns) so
    # canonical_scoring's own family detection never groups them together
    # (two near-identical bare "ID"-only tables would otherwise be judged
    # the same family and one suppressed as a duplicate) -- this test is
    # about MODEL ambiguity, not family-duplicate suppression.
    weak = _Doc("dbo.StuWeak", business_name="Student", description="Roster", column_names=("ID",))
    opaque_a = _Doc("dbo.ZQ88TBL", column_names=("ID", "Alpha"))
    opaque_b = _Doc("dbo.KX55MOD", column_names=("ID", "Beta", "Gamma"))
    docs = [weak, opaque_a, opaque_b]
    catalog = build_semantic_catalog(
        docs,
        enrichment_input=(
            _FakeEnrichment("dbo.ZQ88TBL", business_name="Student", confidence=0.45),
            _FakeEnrichment("dbo.KX55MOD", business_name="Student", confidence=0.44),
        ),
    )
    result = _link("How many students are there", docs, catalog)
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")
    assert entity_req.model_ambiguous is True
    assert entity_req.ambiguous is False  # exactly one REAL structural candidate -- never real ambiguity
    assert {c.object_fqn for c in entity_req.candidates} == {"dbo.StuWeak", "dbo.ZQ88TBL", "dbo.KX55MOD"}


# ── Part 3: advisory column-candidate index ────────────────────────────────


def test_model_measure_role_adds_compatible_numeric_column_candidate():
    documented = _Doc(
        "dbo.Orders", column_names=("Balance",), measure_columns=("Balance",),
        column_descriptors=(_col_descriptor("Balance", "DECIMAL"),),
    )
    opaque = _Doc(
        "dbo.T55", column_names=("Amt",), column_descriptors=(_col_descriptor("Amt", "DECIMAL"),),
    )
    docs = [documented, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(
            _FakeEnrichment("dbo.T55", columns=(_FakeColEnrichment("Amt", measure_role="BALANCE", confidence=0.4),)),
        ),
    )
    result = _link("total balance", docs, catalog)
    measure_req = next(r for r in result.requirements.measure_concepts if r.normalized_label == "balance")
    model_candidates = [c for c in measure_req.candidates if c.match_kind == MATCH_KIND_MODEL_SEMANTIC]
    assert any(c.object_fqn == "dbo.T55" and c.column_name == "Amt" for c in model_candidates)


def test_model_date_role_adds_compatible_date_column_candidate():
    documented = _Doc(
        "dbo.Enrollments", column_names=("CreatedDate",), date_columns=("CreatedDate",),
        column_descriptors=(_col_descriptor("CreatedDate", "DATETIME"),),
    )
    opaque = _Doc(
        "dbo.T77", column_names=("DT1",), column_descriptors=(_col_descriptor("DT1", "DATETIME"),),
    )
    docs = [documented, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(
            _FakeEnrichment("dbo.T77", columns=(_FakeColEnrichment("DT1", date_role="CREATED", confidence=0.4),)),
        ),
    )
    result = _link("show created records", docs, catalog)
    date_req = next(r for r in result.requirements.date_role_concepts if r.normalized_label == "created")
    model_candidates = [c for c in date_req.candidates if c.match_kind == MATCH_KIND_MODEL_SEMANTIC]
    assert any(c.object_fqn == "dbo.T77" and c.column_name == "DT1" for c in model_candidates)


def test_model_dimension_role_adds_compatible_column_candidate():
    documented = _Doc(
        "dbo.Products", column_names=("ProductType",),
        column_descriptors=(_col_descriptor("ProductType", "TEXT"),),
    )
    opaque = _Doc(
        "dbo.T88", column_names=("Cat1",), column_descriptors=(_col_descriptor("Cat1", "TEXT"),),
    )
    docs = [documented, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(
            _FakeEnrichment("dbo.T88", columns=(_FakeColEnrichment("Cat1", dimension_role="TYPE", confidence=0.4),)),
        ),
    )
    result = _link("items by type", docs, catalog)
    dimension_req = next(r for r in result.requirements.dimension_concepts if r.normalized_label == "type")
    model_candidates = [c for c in dimension_req.candidates if c.match_kind == MATCH_KIND_MODEL_SEMANTIC]
    assert any(c.object_fqn == "dbo.T88" and c.column_name == "Cat1" for c in model_candidates)


def test_contradictory_type_role_is_rejected():
    documented = _Doc(
        "dbo.Orders", column_names=("Balance",), measure_columns=("Balance",),
        column_descriptors=(_col_descriptor("Balance", "DECIMAL"),),
    )
    opaque = _Doc(
        "dbo.T99", column_names=("Notes",), column_descriptors=(_col_descriptor("Notes", "TEXT"),),
    )
    docs = [documented, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(
            # A TEXT column claiming measure_role=BALANCE contradicts its
            # own real structural type.
            _FakeEnrichment("dbo.T99", columns=(_FakeColEnrichment("Notes", measure_role="BALANCE", confidence=0.4),)),
        ),
    )
    result = _link("total balance", docs, catalog)
    measure_req = next(r for r in result.requirements.measure_concepts if r.normalized_label == "balance")
    assert all(c.object_fqn != "dbo.T99" for c in measure_req.candidates)


def test_unknown_column_evidence_is_never_indexed():
    documented = _Doc(
        "dbo.Orders", column_names=("Balance",), measure_columns=("Balance",),
        column_descriptors=(_col_descriptor("Balance", "DECIMAL"),),
    )
    opaque = _Doc("dbo.T99", column_names=("RealColumn",), column_descriptors=(_col_descriptor("RealColumn", "DECIMAL"),))
    docs = [documented, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(
            _FakeEnrichment(
                "dbo.T99", columns=(_FakeColEnrichment("GhostColumn", measure_role="BALANCE", confidence=0.4),),
            ),
        ),
    )
    assert catalog.model_inferred_column_evidence == ()  # rejected at the semantic_catalog layer already
    result = _link("total balance", docs, catalog)
    measure_req = next(r for r in result.requirements.measure_concepts if r.normalized_label == "balance")
    assert all(c.object_fqn != "dbo.T99" for c in measure_req.candidates)


# ── Part 5: candidate scoring ────────────────────────────────────────────


def test_anchor_model_semantic_score_is_separately_observable_and_bounded():
    weak = _Doc("dbo.StuWeak", business_name="Student", description="Roster", column_names=("ID",))
    opaque = _Doc("dbo.T1", column_names=("ID",))
    docs = [weak, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Student", confidence=0.4),),
    )
    # A real, independent lexical signal (e.g. this table also surfaced in
    # a lexical/vector search for other reasons) is combined WITH model
    # evidence here -- without ANY other evidence at all, a capped 0.50
    # model score at its own small weight is deliberately too little to
    # clear the ambiguity-preservation margin on its own (Part 5: "do not
    # let the 0.50 model score dominate confirmed evidence") and would
    # never surface as a selected anchor purely by itself.
    result = _link(
        "How many students are there", docs, catalog, lexical_rankings=[_RankedHit("dbo.T1", 3.0)],
    )
    opaque_anchor = _anchor(result, "dbo.T1")
    assert opaque_anchor is not None
    assert 0.0 < opaque_anchor.model_semantic_score <= MODEL_CANDIDATE_CONFIDENCE_CAP
    assert opaque_anchor.catalog_score == 0.0  # zero STRUCTURAL support for dbo.T1
    assert ADMISSION_REASON_MODEL_MATCH in opaque_anchor.admission_reasons


def test_model_weight_is_the_smallest_and_cannot_dominate_pure_model_evidence():
    from core.sqlservice.schema_linker import (
        ANCHOR_WEIGHT_CANONICAL, ANCHOR_WEIGHT_CATALOG, ANCHOR_WEIGHT_EXAMPLE,
        ANCHOR_WEIGHT_LEXICAL, ANCHOR_WEIGHT_SEMANTIC,
    )
    weights = [ANCHOR_WEIGHT_CATALOG, ANCHOR_WEIGHT_LEXICAL, ANCHOR_WEIGHT_EXAMPLE, ANCHOR_WEIGHT_SEMANTIC, ANCHOR_WEIGHT_CANONICAL]
    assert ANCHOR_WEIGHT_MODEL_SEMANTIC < min(weights)
    assert abs(sum(weights) + ANCHOR_WEIGHT_MODEL_SEMANTIC - 1.0) < 1e-9
    # Even at the maximum possible model score (the confidence cap), its
    # weighted contribution alone stays small -- "do not let the 0.50
    # model score dominate confirmed evidence" (Part 5).
    assert ANCHOR_WEIGHT_MODEL_SEMANTIC * MODEL_CANDIDATE_CONFIDENCE_CAP < 0.05

    # A strong, fully-confirmed structural anchor is never overtaken purely
    # by a competing object's maximal model score WITH NO OTHER EVIDENCE AT
    # ALL -- the model-only competitor here doesn't even clear the
    # ambiguity-preservation margin against it, so it never becomes a
    # selected anchor by model evidence alone (see test_dictionary_
    # candidate_outranks_model_only_candidate_regardless_of_raw_confidence
    # for the requirement-level tiering guarantee that holds unconditionally).
    documented = _Doc("dbo.Stu", business_name="Student", description="Roster", column_names=("ID",))
    model_only = _Doc("dbo.T1", column_names=("ID",))
    docs = [documented, model_only]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Student", confidence=0.99),),
    )
    result = _link("How many students are there", docs, catalog)
    documented_anchor = _anchor(result, "dbo.Stu")
    assert documented_anchor is not None
    assert _anchor(result, "dbo.T1") is None  # never surfaces as an anchor from model evidence alone


# ── Part 6: planner safety boundary ─────────────────────────────────────────


def test_model_evidence_types_carry_no_join_capable_fields():
    object_fields = {f.name for f in dataclasses.fields(RequirementCandidate)}
    anchor_fields = {f.name for f in dataclasses.fields(AnchorCandidate)}
    forbidden = ("join", "sql", "filter", "literal", "condition", "edge")
    for field_name in object_fields | anchor_fields:
        assert not any(tok in field_name.lower() for tok in forbidden), field_name


def test_join_graph_cache_remains_reusable_when_relationships_unchanged():
    doc_a = _Doc("dbo.Stu", column_names=("ID",))
    doc_b = _Doc("dbo.Course", column_names=("ID",))
    docs = [doc_a, doc_b]
    prepare_join_graph_index(docs)
    assert join_planner_cache_size() == 1
    prepare_join_graph_index(docs)  # unrelated to any catalog/enrichment content
    assert join_planner_cache_size() == 1  # reused, never duplicated


# ── Part 7: catalog/index identity ──────────────────────────────────────────


def test_linker_index_cache_invalidates_when_model_evidence_changes():
    doc = _Doc("dbo.Stu", business_name="Student", column_names=("ID",))
    other = _Doc("dbo.T1", column_names=("ID",))
    docs = [doc, other]

    baseline_catalog = build_semantic_catalog(docs)
    prepare_linker_index(baseline_catalog, docs)
    assert schema_linker_cache_size() == 1

    enriched_catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Student", confidence=0.4),),
    )
    assert enriched_catalog.fingerprint != baseline_catalog.fingerprint
    prepare_linker_index(enriched_catalog, docs)
    assert schema_linker_cache_size() == 2  # a genuinely new entry, never reused from the un-enriched one

    baseline_result = _link("How many students are there", docs, baseline_catalog)
    enriched_result = _link("How many students are there", docs, enriched_catalog)
    baseline_entity_req = next(r for r in baseline_result.requirements.entity_concepts if r.normalized_label == "student")
    enriched_entity_req = next(r for r in enriched_result.requirements.entity_concepts if r.normalized_label == "student")
    assert {c.object_fqn for c in baseline_entity_req.candidates} == {"dbo.Stu"}
    assert {c.object_fqn for c in enriched_entity_req.candidates} == {"dbo.Stu", "dbo.T1"}


# ── Deep immutability / determinism / hygiene ──────────────────────────────


def test_model_evidence_results_are_deeply_immutable():
    weak = _Doc("dbo.StuWeak", business_name="Student", description="Roster", column_names=("ID",))
    opaque = _Doc("dbo.T1", column_names=("ID",))
    docs = [weak, opaque]
    catalog = build_semantic_catalog(
        docs, enrichment_input=(_FakeEnrichment("dbo.T1", business_name="Student", confidence=0.4),),
    )
    result = _link(
        "How many students are there", docs, catalog, lexical_rankings=[_RankedHit("dbo.T1", 3.0)],
    )
    entity_req = next(r for r in result.requirements.entity_concepts if r.normalized_label == "student")

    with pytest.raises(dataclasses.FrozenInstanceError):
        entity_req.model_ambiguous = True
    with pytest.raises(dataclasses.FrozenInstanceError):
        entity_req.candidates[0].confidence = 1.0

    anchor = _anchor(result, "dbo.T1")
    assert anchor is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        anchor.model_semantic_score = 1.0
    assert isinstance(entity_req.candidates, tuple)


def test_deterministic_under_shuffled_documents_and_enrichment_order():
    weak = _Doc("dbo.StuWeak", business_name="Student", column_names=("ID",))
    opaque_a = _Doc("dbo.T1", column_names=("ID",))
    opaque_b = _Doc("dbo.T2", column_names=("ID",))
    docs = [weak, opaque_a, opaque_b]
    enrichment = (
        _FakeEnrichment("dbo.T1", business_name="Student", confidence=0.40),
        _FakeEnrichment("dbo.T2", business_name="Student", confidence=0.35),
    )

    docs_shuffled = list(docs)
    random.Random(42).shuffle(docs_shuffled)
    enrichment_shuffled = tuple(reversed(enrichment))

    catalog_a = build_semantic_catalog(docs, enrichment_input=enrichment)
    catalog_b = build_semantic_catalog(docs_shuffled, enrichment_input=enrichment_shuffled)
    assert catalog_a.fingerprint == catalog_b.fingerprint

    result_a = _link("How many students are there", docs, catalog_a)
    result_b = _link("How many students are there", docs_shuffled, catalog_b)
    assert result_a.requirement_fingerprint == result_b.requirement_fingerprint
    assert result_a.fingerprint == result_b.fingerprint


def test_no_database_or_provider_io_in_schema_linker_module():
    import core.sqlservice.schema_linker as module
    source = inspect.getsource(module)
    for forbidden in ("import sqlite3", "import openai", "get_connection", "requests."):
        assert forbidden not in source


def test_no_reference_specific_tokens_in_schema_linker_module():
    import core.sqlservice.schema_linker as module
    source = inspect.getsource(module)
    for forbidden in ("EXAMPLECO", "90004", "ACHTransaction"):
        assert forbidden not in source


# ── Synthetic architecture test ─────────────────────────────────────────────


def test_synthetic_opaque_table_wins_the_entity_slot_via_combined_evidence_and_plans_safely():
    """
    Generic schema where the question explicitly asks about "students" and
    "course": deterministic metadata alone maps "student" only to a weak,
    secondary object (dbo.StudentMisc, business_name="Student"). The
    CORRECT object (dbo.ZQ88TBL) has a fully opaque technical name --
    structurally invisible to the "student" concept -- but carries real,
    independent structural evidence of its own (a real "Balance" measure
    column, real lexical relevance, and a confirmed FK relationship to
    dbo.Course). Once validated model enrichment gives dbo.ZQ88TBL the
    matching business alias, the requirement set is UNCHANGED, dbo.ZQ88TBL
    becomes an additional candidate for the SAME "student" requirement,
    and its COMBINED evidence (real structural + lexical + model) lets it
    outrank the secondary object for the anchor slot -- and its confirmed
    relationship lets the join planner bridge it to dbo.Course safely,
    with a REAL edge, never a fabricated one.
    """
    secondary = _Doc(
        "dbo.StudentMisc", retrieval_tier=2, business_name="Student",
        column_names=("ID",), primary_key_columns=("ID",), has_primary_key=True,
        column_descriptors=(_col_descriptor("ID", "INTEGER", pk=True),),
    )
    course = _Doc(
        "dbo.Course", business_name="Course", description="Course catalog",
        column_names=("CourseID",), primary_key_columns=("CourseID",), has_primary_key=True,
        column_descriptors=(_col_descriptor("CourseID", "INTEGER", pk=True),),
    )
    opaque = _Doc(
        "dbo.ZQ88TBL", retrieval_tier=1,
        column_names=("ID", "Balance", "CourseID"), primary_key_columns=("ID",), has_primary_key=True,
        measure_columns=("Balance",),
        foreign_key_targets=("dbo.Course.CourseID",),
        confirmed_relationship_targets=("dbo.Course.CourseID",),
        confirmed_relationship_edges=(
            {"direction": "outbound", "column": "CourseID", "other": "dbo.Course.CourseID", "kind": "PHYSICAL_FK"},
        ),
        column_descriptors=(
            _col_descriptor("ID", "INTEGER", pk=True),
            _col_descriptor("Balance", "DECIMAL"),
            _col_descriptor("CourseID", "INTEGER", fk=True),
        ),
    )
    docs = [secondary, course, opaque]
    question = "total balance for students by course"
    lexical_rankings = [_RankedHit("dbo.ZQ88TBL", 3.0)]

    baseline_catalog = build_semantic_catalog(docs)
    baseline = _link(question, docs, baseline_catalog, lexical_rankings=lexical_rankings)

    baseline_entity_req = next(r for r in baseline.requirements.entity_concepts if r.normalized_label == "student")
    assert baseline_entity_req.candidates[0].object_fqn == "dbo.StudentMisc"  # deterministic metadata alone: wrong/secondary object
    baseline_keys = _core_constraint_keys(baseline.requirements)

    enriched_catalog = build_semantic_catalog(
        docs, enrichment_input=(
            _FakeEnrichment("dbo.ZQ88TBL", business_name="Student", aliases=("learner",), confidence=0.45),
        ),
    )
    enriched = _link(question, docs, enriched_catalog, lexical_rankings=lexical_rankings)

    # The requirement set is IDENTICAL -- the question's own meaning never changed.
    assert _core_constraint_keys(enriched.requirements) == baseline_keys
    assert enriched.requirement_fingerprint == baseline.requirement_fingerprint

    # dbo.ZQ88TBL is now an additional candidate for the SAME "student" requirement.
    enriched_entity_req = next(r for r in enriched.requirements.entity_concepts if r.normalized_label == "student")
    assert {c.object_fqn for c in enriched_entity_req.candidates} == {"dbo.StudentMisc", "dbo.ZQ88TBL"}
    model_candidate = next(c for c in enriched_entity_req.candidates if c.object_fqn == "dbo.ZQ88TBL")
    assert model_candidate.match_kind == MATCH_KIND_MODEL_SEMANTIC
    assert model_candidate.confidence <= MODEL_CANDIDATE_CONFIDENCE_CAP

    # It OUTRANKS the weak secondary object through combined evidence
    # (structural measure + real lexical relevance + model entity support).
    enriched_opaque_anchor = _anchor(enriched, "dbo.ZQ88TBL")
    assert enriched_opaque_anchor is not None
    enriched_secondary_anchor = _anchor(enriched, "dbo.StudentMisc")
    secondary_score = enriched_secondary_anchor.combined_score if enriched_secondary_anchor else 0.0
    assert enriched_opaque_anchor.combined_score > secondary_score

    # A confirmed relationship lets the planner bridge it to dbo.Course
    # SAFELY -- a real edge, never a fabricated one; no model evidence is
    # ever consulted by the join planner at all.
    plan_result = plan_schema_join_subgraph(enriched, docs)
    assert plan_result.primary_plan is not None
    assert "dbo.ZQ88TBL" in plan_result.primary_plan.selected_object_fqns
    assert "dbo.Course" in plan_result.primary_plan.selected_object_fqns
    assert len(plan_result.primary_plan.join_edges) == 1
    edge = plan_result.primary_plan.join_edges[0]
    assert {edge.from_object_fqn, edge.to_object_fqn} == {"dbo.ZQ88TBL", "dbo.Course"}
    assert edge.relationship_type == "PHYSICAL_FK"  # the REAL confirmed edge, never fabricated
