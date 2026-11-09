"""
Focused test — SQL Architecture Correction, Phase 6, Step 4C2, requirement
9: OBJECT_CORE results feed the Step 4B1 advisory candidate index exactly
like the pre-existing COLUMN_DEFERRED contract's model evidence already
does, with ZERO changes required to core.sqlservice.semantic_catalog or
core.sqlservice.schema_linker.

Verifies, end to end, against real (in-memory, no database) production
code:
  1. build_semantic_catalog() accepts OBJECT_CORE-shaped
     StoredSemanticEnrichment rows (columns=(), supported_operations=())
     with no code change and produces real model_inferred_object_evidence.
  2. schema_linker.link_question_to_schema() surfaces an OBJECT_CORE alias
     as an ADDITIONAL MATCH_KIND_MODEL_SEMANTIC candidate under an
     ALREADY-matched (structurally real) entity requirement — an opaque
     object with zero structural/lexical signal for the question becomes
     visible only because of its OBJECT_CORE alias.
  3. The model candidate's score is separately bounded (<=
     MODEL_CANDIDATE_CONFIDENCE_CAP) and never promotes the requirement's
     own `confidence` (computed only over structural candidates).
  4. The question's requirement_fingerprint is IDENTICAL with and without
     the OBJECT_CORE enrichment present — model evidence can only add
     candidates under an existing requirement, never seed a new one.
  5. core.sqlservice.join_planner.prepare_join_graph_index() takes ONLY
     `documents` (no catalog/semantic-evidence parameter at all) — model
     evidence structurally CANNOT reach join-edge authorization.

No real provider, no database, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_linker_compatibility.py -v
"""

from __future__ import annotations

import hashlib
import inspect
from types import MappingProxyType

from core.sqlservice.join_planner import prepare_join_graph_index
from core.sqlservice.retrieval_documents import RetrievalDocument
from core.sqlservice.schema_linker import (
    MATCH_KIND_MODEL_SEMANTIC,
    MODEL_CANDIDATE_CONFIDENCE_CAP,
    STATUS_OK,
    clear_schema_linker_cache,
    link_question_to_schema,
)
from core.sqlservice.semantic_catalog import build_semantic_catalog
from core.sqlservice.semantic_enrichment_service import (
    EnrichedObjectSemantics,
    EvidenceItem,
    MODEL_INFERRED_CONFIDENCE_CAP,
    OBJECT_ROLE_ENTITY,
    PROVENANCE_MODEL_INFERRED_SEMANTIC,
    _THE_ENRICHMENT_GUARD,
)


def _doc(object_fqn, *, columns=(("ID", "INTEGER", True, False),), tier=1, business_name=None):
    schema_name, object_name = object_fqn.split(".", 1)
    column_descriptors = tuple(
        {"column_name": name, "normalized_sql_type": sql_type, "is_primary_key": pk, "is_foreign_key": fk}
        for name, sql_type, pk, fk in columns
    )
    metadata = {
        "column_descriptors": column_descriptors,
        "primary_key_columns": tuple(c[0] for c in columns if c[2]),
        "confirmed_relationship_edges": (),
        "status_columns": (), "date_columns": (), "measure_columns": (),
        "business_name": business_name, "description": None, "synonyms": (),
    }
    text = f"OBJECT: {object_fqn}\nCOLUMNS:\n" + "\n".join(f"  {c[0]}" for c in columns)
    doc_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return RetrievalDocument(
        source_id=1, object_fqn=object_fqn, object_type="TABLE",
        schema_name=schema_name, object_name=object_name, retrieval_tier=tier,
        document_text=text, document_hash=doc_hash, metadata=MappingProxyType(metadata),
    )


def _object_core_enrichment(object_fqn: str, *, aliases: tuple[str, ...], confidence: float = 0.9):
    """A REAL, guard-constructed EnrichedObjectSemantics shaped exactly like
    what the OBJECT_CORE contract validator produces (columns=(),
    supported_operations=()) — the only way to legitimately obtain one from
    outside core.sqlservice.semantic_enrichment_service is via its own
    strict validation, which _validate_object_core_batch_response already
    tests directly; this constructs the resulting VALUE the same way that
    validator does, to keep this test focused purely on DOWNSTREAM
    (catalog/linker) consumption."""
    return EnrichedObjectSemantics(
        object_fqn=object_fqn, business_name="Opaque Table", description="An opaque table.",
        object_role=OBJECT_ROLE_ENTITY, entity_concepts=("student",), aliases=aliases,
        supported_operations=(), confidence=min(confidence, MODEL_INFERRED_CONFIDENCE_CAP),
        provenance=(EvidenceItem(kind=PROVENANCE_MODEL_INFERRED_SEMANTIC, detail=object_fqn),),
        _gate_guard=_THE_ENRICHMENT_GUARD,
    )


class _StoredLike:
    """Duck-typed StoredSemanticEnrichment stand-in — build_semantic_catalog
    only ever reads object_fqn/business_name/description/object_role/
    entity_concepts/aliases/confidence/provenance/.columns (see that
    module's own duck-typed contract comment); using the REAL dataclass
    from core.sqlservice.semantic_enrichment_store would require a live
    database round-trip this test doesn't need."""

    def __init__(self, enriched: EnrichedObjectSemantics):
        self.object_fqn = enriched.object_fqn
        self.business_name = enriched.business_name
        self.description = enriched.description
        self.object_role = enriched.object_role
        self.entity_concepts = enriched.entity_concepts
        self.aliases = enriched.aliases
        self.supported_operations = enriched.supported_operations
        self.confidence = enriched.confidence
        self.provenance = enriched.provenance
        self.columns = ()  # OBJECT_CORE never persists column-level semantics


def setup_function(_fn):
    clear_schema_linker_cache()


def test_object_core_alias_surfaces_as_additional_model_candidate_without_changing_fingerprint():
    student_doc = _doc("dbo.Student", columns=(("StuID", "INTEGER", True, False),))
    opaque_doc = _doc("dbo.Opaque1", columns=(("OpaqueID", "INTEGER", True, False),))
    documents = [student_doc, opaque_doc]

    catalog_without_enrichment = build_semantic_catalog(documents)
    catalog_with_enrichment = build_semantic_catalog(
        documents,
        enrichment_input=[_StoredLike(_object_core_enrichment("dbo.Opaque1", aliases=("student",)))],
    )

    # 1. build_semantic_catalog accepted the OBJECT_CORE-shaped row with
    #    zero code changes and produced real model evidence for it.
    assert any(e.object_fqn == "dbo.Opaque1" for e in catalog_with_enrichment.model_inferred_object_evidence)
    assert catalog_without_enrichment.model_inferred_object_evidence == ()

    question = "How many students are there?"

    result_without = link_question_to_schema(question, catalog_without_enrichment, documents)
    result_with = link_question_to_schema(question, catalog_with_enrichment, documents)
    assert result_without.status == STATUS_OK
    assert result_with.status == STATUS_OK

    def _student_requirement(result):
        return next(
            (r for r in result.requirements.entity_concepts if r.normalized_label.lower() == "student"), None,
        )

    req_without = _student_requirement(result_without)
    req_with = _student_requirement(result_with)
    assert req_without is not None and req_with is not None

    without_fqns = {c.object_fqn for c in req_without.candidates}
    with_fqns_and_kinds = {(c.object_fqn, c.match_kind) for c in req_with.candidates}

    # 2. dbo.Opaque1 has NO structural/lexical connection to "student" at
    #    all -- it only ever appears once the OBJECT_CORE alias is present.
    assert "dbo.Opaque1" not in without_fqns
    assert ("dbo.Opaque1", MATCH_KIND_MODEL_SEMANTIC) in with_fqns_and_kinds

    model_candidate = next(c for c in req_with.candidates if c.object_fqn == "dbo.Opaque1")
    # 3. Separately bounded, never able to outrank/promote the requirement's
    #    own confidence (computed only over structural candidates).
    assert model_candidate.confidence <= MODEL_CANDIDATE_CONFIDENCE_CAP
    assert req_with.confidence == req_without.confidence

    # 4. Adding the OBJECT_CORE candidate never changes WHICH normalized
    #    requirements were matched -- the fingerprint is identical.
    assert result_with.requirement_fingerprint == result_without.requirement_fingerprint


def test_join_planner_signature_proves_model_evidence_cannot_reach_join_authorization():
    sig = inspect.signature(prepare_join_graph_index)
    param_names = set(sig.parameters.keys())
    # ONLY `documents` (+ the cache toggle) -- no catalog, no
    # SchemaLinkingResult, no semantic-evidence parameter of any kind exists
    # for a model-inferred candidate to travel through into a join edge.
    assert param_names == {"documents", "use_cache"}
