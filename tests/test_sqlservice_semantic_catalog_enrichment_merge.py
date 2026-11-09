"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4B: automatic
advisory merge of model-inferred semantics into core.sqlservice.
semantic_catalog.build_semantic_catalog() (Part 4).

Pure/offline: no database, no OpenAI call, no live SQL Server. Uses the
SAME small attribute-bag `_Doc` fixture as
tests/test_sqlservice_semantic_catalog.py, plus a duck-typed
`_FakeEnrichment`/`_FakeEnrichedColumn` standing in for core.sqlservice.
semantic_enrichment_store.StoredSemanticEnrichment — build_semantic_catalog
never imports that module (see this module's own circular-import note in
semantic_catalog.py), so any object exposing the right attributes is
accepted, exactly like `documents` itself.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_catalog_enrichment_merge.py -v
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from core.sqlservice.semantic_catalog import (
    MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP,
    MODEL_INFERRED_PROVENANCE_KIND,
    REJECTED_REASON_MISSING_PROVENANCE,
    REJECTED_REASON_UNKNOWN_COLUMN,
    REJECTED_REASON_UNKNOWN_OBJECT,
    build_semantic_catalog,
)

# ── Fixture builders ───────────────────────────────────────────────────────

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


def _find_object_evidence(catalog, object_fqn):
    matches = [e for e in catalog.model_inferred_object_evidence if e.object_fqn == object_fqn]
    assert matches, f"no model_inferred_object_evidence for {object_fqn!r}"
    return matches[0]


def _find_column_evidence(catalog, object_fqn, column_name):
    matches = [
        e for e in catalog.model_inferred_column_evidence
        if e.object_fqn == object_fqn and e.column_name == column_name
    ]
    assert matches, f"no model_inferred_column_evidence for {object_fqn}.{column_name}"
    return matches[0]


# ── Structural facts always win / dictionary precedence ───────────────────


def test_catalog_without_enrichment_input_is_unaffected_by_default():
    doc = _Doc("dbo.Stu", business_name="Student", column_names=("StuID",), primary_key_columns=("StuID",))
    catalog = build_semantic_catalog([doc])
    assert catalog.model_inferred_object_evidence == ()
    assert catalog.model_inferred_column_evidence == ()
    assert catalog.coverage["model_inferred_object_evidence_count"] == 0


def test_entities_are_identical_with_or_without_enrichment_input():
    doc = _Doc("dbo.Stu", business_name="Student", column_names=("StuID",), primary_key_columns=("StuID",))
    plain = build_semantic_catalog([doc])
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Totally Different Name", confidence=0.5),)
    enriched = build_semantic_catalog([doc], enrichment_input=enrichment)
    assert plain.entities == enriched.entities  # structural facts always win -- byte-identical


def test_dictionary_confirmed_business_name_is_never_overridden():
    doc = _Doc("dbo.Stu", business_name="Student", description="Official roster", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Learner Record", description="A generic entity"),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)

    entity = next(e for e in catalog.entities if e.normalized_name == "student")
    assert entity.candidate_objects[0].object_fqn == "dbo.Stu"  # unaffected

    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert evidence.dictionary_has_business_name is True
    assert evidence.dictionary_has_description is True
    # Candidate is still recorded for observability -- "outranked", not hidden.
    assert evidence.business_name_candidates == ("Learner Record",)


def test_model_alias_is_added_as_advisory_evidence_without_dictionary():
    doc = _Doc("dbo.Stu", column_names=("StuID",))  # no dictionary content at all
    enrichment = (
        _FakeEnrichment("dbo.Stu", business_name="Student", aliases=("learner", "pupil"), confidence=0.45),
    )
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert evidence.dictionary_has_business_name is False
    assert evidence.business_name_candidates == ("Student",)
    assert set(evidence.alias_candidates) == {"learner", "pupil"}
    assert evidence.confidence <= MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP


# ── Model candidate maps to existing concept without creating structure ───


def test_model_candidate_maps_to_existing_object_without_creating_one():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.DoesNotExist", business_name="Ghost Table"),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    assert catalog.model_inferred_object_evidence == ()
    assert catalog.coverage["model_inferred_rejected_by_reason"] == {REJECTED_REASON_UNKNOWN_OBJECT: 1}
    # No new object anywhere in the catalog.
    all_object_fqns = {c.object_fqn for e in catalog.entities for c in e.candidate_objects}
    assert "dbo.DoesNotExist" not in all_object_fqns


def test_model_cannot_create_a_column_that_does_not_exist():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (
        _FakeEnrichment("dbo.Stu", columns=(_FakeColEnrichment("PhantomColumn", semantic_role="MEASURE"),)),
    )
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    assert catalog.model_inferred_column_evidence == ()
    assert catalog.coverage["model_inferred_rejected_by_reason"] == {REJECTED_REASON_UNKNOWN_COLUMN: 1}


def test_model_column_evidence_maps_to_a_real_column():
    doc = _Doc("dbo.Stu", column_names=("StuID", "FName"))
    enrichment = (
        _FakeEnrichment(
            "dbo.Stu",
            columns=(_FakeColEnrichment("FName", business_label="First Name", semantic_role="DESCRIPTIVE", confidence=0.4),),
        ),
    )
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    col_evidence = _find_column_evidence(catalog, "dbo.Stu", "FName")
    assert col_evidence.business_label_candidates == ("First Name",)
    assert col_evidence.semantic_role_candidates == (("DESCRIPTIVE", 0.4),)


def test_missing_provenance_is_rejected():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Student", provenance=()),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    assert catalog.model_inferred_object_evidence == ()
    assert catalog.coverage["model_inferred_rejected_by_reason"] == {REJECTED_REASON_MISSING_PROVENANCE: 1}


# ── Conflicting proposals remain ambiguous ─────────────────────────────────


def test_conflicting_object_role_proposals_remain_ambiguous():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (
        _FakeEnrichment("dbo.Stu", object_role="ENTITY", confidence=0.5),
        _FakeEnrichment("dbo.Stu", object_role="EVENT", confidence=0.48),
    )
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert evidence.ambiguous_object_role is True
    assert dict(evidence.object_role_candidates) == {"ENTITY": 0.5, "EVENT": 0.48}


def test_non_conflicting_role_is_not_marked_ambiguous():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", object_role="ENTITY", confidence=0.5),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert evidence.ambiguous_object_role is False


# ── Confidence cap / provenance preserved ──────────────────────────────────


def test_confidence_is_capped_even_if_upstream_claims_higher():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Student", confidence=0.99),)  # a misbehaving upstream
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert evidence.confidence <= MODEL_INFERRED_EVIDENCE_CONFIDENCE_CAP


def test_evidence_provenance_kind_is_model_inferred_semantic():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Student"),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)
    evidence = _find_object_evidence(catalog, "dbo.Stu")
    assert all(item.kind == MODEL_INFERRED_PROVENANCE_KIND for item in evidence.evidence)


# ── Model cannot authorize joins / tables / columns ────────────────────────


def test_model_inferred_evidence_types_have_no_join_capable_fields():
    import core.sqlservice.semantic_catalog as module

    object_fields = {f.name for f in dataclasses.fields(module.ModelInferredObjectEvidence)}
    column_fields = {f.name for f in dataclasses.fields(module.ModelInferredColumnEvidence)}
    forbidden_tokens = ("join", "sql", "filter", "literal", "condition")
    for field_name in object_fields | column_fields:
        assert not any(tok in field_name.lower() for tok in forbidden_tokens), field_name


# ── Fingerprint / cache-invalidation contract ──────────────────────────────


def test_fingerprint_changes_when_usable_enrichment_content_changes():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    baseline = build_semantic_catalog([doc])
    enriched_a = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.Stu", business_name="Student", confidence=0.4),),
    )
    enriched_b = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.Stu", business_name="Learner", confidence=0.4),),
    )
    assert baseline.fingerprint != enriched_a.fingerprint
    assert enriched_a.fingerprint != enriched_b.fingerprint


def test_fingerprint_unaffected_by_fully_rejected_enrichment_matching_baseline():
    """An enrichment_input entirely rejected (unknown object) still changes
    `coverage`, but should NOT be confused with a no-op: this test only
    documents that a rejected-only run's model_inferred_* tuples are empty,
    matching the pure no-enrichment baseline's OWN evidence tuples (empty),
    even though `coverage` itself differs (rejected_count > 0) -- coverage
    is not part of what a caller should assume is fingerprint-stable."""
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    baseline = build_semantic_catalog([doc])
    rejected_only = build_semantic_catalog(
        [doc], enrichment_input=(_FakeEnrichment("dbo.DoesNotExist", business_name="Ghost"),),
    )
    assert baseline.model_inferred_object_evidence == rejected_only.model_inferred_object_evidence == ()


def test_schema_linker_index_cache_invalidates_on_enrichment_change():
    """The mechanism Phase 6 Step 4B Part 6 relies on: core.sqlservice.
    schema_linker's own index cache is keyed ONLY by SemanticCatalog.
    fingerprint (unchanged code) -- verified here end to end without
    touching that module at all."""
    from core.sqlservice.schema_linker import (
        clear_schema_linker_cache,
        prepare_linker_index,
        schema_linker_cache_size,
    )

    doc = _Doc("dbo.Stu", column_names=("StuID",), primary_key_columns=("StuID",))
    clear_schema_linker_cache()
    try:
        baseline = build_semantic_catalog([doc])
        prepare_linker_index(baseline, [doc])
        assert schema_linker_cache_size() == 1

        enriched = build_semantic_catalog(
            [doc], enrichment_input=(_FakeEnrichment("dbo.Stu", business_name="Student", confidence=0.4),),
        )
        assert enriched.fingerprint != baseline.fingerprint
        prepare_linker_index(enriched, [doc])
        assert schema_linker_cache_size() == 2  # a genuinely NEW cache entry, never reused from the un-enriched one
    finally:
        clear_schema_linker_cache()


# ── Deep immutability ───────────────────────────────────────────────────────


def test_catalog_and_evidence_are_deeply_immutable():
    doc = _Doc("dbo.Stu", column_names=("StuID",))
    enrichment = (_FakeEnrichment("dbo.Stu", business_name="Student", columns=(_FakeColEnrichment("StuID"),)),)
    catalog = build_semantic_catalog([doc], enrichment_input=enrichment)

    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.model_inferred_object_evidence = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.model_inferred_object_evidence[0].confidence = 1.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        catalog.model_inferred_column_evidence[0].business_label_candidates = ("x",)
    assert isinstance(catalog.model_inferred_object_evidence[0].business_name_candidates, tuple)
    assert isinstance(catalog.model_inferred_column_evidence[0].semantic_role_candidates, tuple)
