"""
Focused unit tests — Phase 7, Step 4, Part 2: the generic pre-generation
ambiguity guard (core/sqlservice/ambiguity_guard.py).

No database, no AI provider, no real OpenAI call anywhere in this file —
check_ambiguity() is pure/local, so every fixture below is constructed
directly in memory. core.sqlservice.ambiguity_guard.load_business_knowledge_
evidence and BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED are monkeypatched per test
rather than touching the real business-knowledge tables.

Run:
    venv/Scripts/pytest tests/test_sqlservice_ambiguity_guard.py -v
"""
import hashlib
from types import MappingProxyType

import pytest

import core.sqlservice.ambiguity_guard as ambiguity_guard_module
from core.sqlservice.ambiguity_guard import check_ambiguity
from core.sqlservice.business_knowledge_evidence import (
    BusinessKnowledgeEvidenceIdentity,
    BusinessKnowledgeEvidenceResult,
    BusinessKnowledgeMappingEvidence,
)
from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_SEMANTIC,
    STATUS_OK as CONTEXT_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.schema_relationship_completion import STATUS_CONNECTED, RelationshipCompletionResult
from core.sqlservice.schema_selection_service import (
    RESPONSE_STATUS_SELECTED,
    IntentSummary,
    ObjectSelectionReason,
    SchemaSelectionCandidate,
)

SOURCE_ID = 90004
USER_ID = "user-1"


def _intent(operation="AGGREGATE", entities=("student",), measures=("amount",), filters=(), dates=(), groupings=()):
    return IntentSummary(
        operation=operation, entities=entities, measures=measures, filters=filters, dates=dates, groupings=groupings,
    )


def _candidate(selected_object_fqns, object_reasons, *, entities=("student",), measures=("amount",)):
    return SchemaSelectionCandidate(
        response_status=RESPONSE_STATUS_SELECTED,
        intent_summary=_intent(entities=entities, measures=measures),
        selected_object_fqns=tuple(selected_object_fqns),
        object_reasons=tuple(object_reasons),
        clarification_question=None,
    )


def _relationship_result(selected_object_fqns, connector_object_fqns=()):
    return RelationshipCompletionResult(
        status=STATUS_CONNECTED,
        selected_object_fqns=tuple(selected_object_fqns),
        connector_object_fqns=tuple(connector_object_fqns),
        warnings=(),
        error=None,
    )


def _context_document(fqn, *, date_columns=(), score=0.9):
    text = f"OBJECT: {fqn}\nTYPE: TABLE\nTIER: 1\nCOLUMNS:\n  id INTEGER [PK]"
    return ContextDocument(
        object_fqn=fqn, object_type="TABLE", retrieval_tier=1,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType({"date_columns": tuple(date_columns)}),
        score=score, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _schema_context(documents, *, build_id=1, schema_fingerprint="fp-123"):
    docs = tuple(documents)
    context_text = "\n\n".join(d.document_text for d in docs)
    return SchemaContextResult(
        status=CONTEXT_STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536,
        documents=docs, context_text=context_text, total_characters=len(context_text),
        truncated=False, warnings=(), error=None,
    )


def _evidence_row(concept_id, role, status, evidence=()):
    return BusinessKnowledgeMappingEvidence(
        concept_id=concept_id, concept_type="MEASURE", concept_origin="AUTHORITATIVE",
        mapping_role=role, mapping_status=status, confidence=0.9, evidence=tuple(evidence),
    )


def _mock_evidence(monkeypatch, mappings_by_object_fqn_lower):
    monkeypatch.setattr(ambiguity_guard_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", True)
    result = BusinessKnowledgeEvidenceResult(
        identity=BusinessKnowledgeEvidenceIdentity(package_id=1, schema_fingerprint="fp-123"),
        mappings_by_object_fqn_lower=mappings_by_object_fqn_lower,
    )
    monkeypatch.setattr(ambiguity_guard_module, "load_business_knowledge_evidence", lambda *a, **k: result)


# ── No signal at all — never falsely flags ordinary questions ─────────────


def test_no_evidence_no_ambiguity():
    reason = ObjectSelectionReason(
        object_fqn="dbo.enrollments", supported_requirements=("student count",), supporting_columns=("student_id",),
    )
    candidate = _candidate(["dbo.enrollments"], [reason])
    relationship_result = _relationship_result(["dbo.enrollments"])
    schema_context = _schema_context([_context_document("dbo.enrollments")])

    result = check_ambiguity(
        "How many students are enrolled?", SOURCE_ID, USER_ID, candidate, relationship_result, schema_context,
        "fp-123",
    )
    assert result.is_ambiguous is False
    assert result.clarification_question is None


def test_business_knowledge_evidence_disabled_is_not_touched(monkeypatch):
    """Flag off (the default) must behave identically to no evidence at all
    -- load_business_knowledge_evidence must never even be called."""
    called = {"count": 0}

    def _blow_up(*_a, **_k):
        called["count"] += 1
        raise AssertionError("must not be called when the flag is off")

    monkeypatch.setattr(ambiguity_guard_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", False)
    monkeypatch.setattr(ambiguity_guard_module, "load_business_knowledge_evidence", _blow_up)

    reason = ObjectSelectionReason(
        object_fqn="dbo.payments", supported_requirements=("total paid",), supporting_columns=("amount",),
    )
    candidate = _candidate(["dbo.payments"], [reason])
    result = check_ambiguity(
        "What is the total paid?", SOURCE_ID, USER_ID, candidate, _relationship_result(["dbo.payments"]),
        _schema_context([_context_document("dbo.payments")]), "fp-123",
    )
    assert called["count"] == 0
    assert result.is_ambiguous is False


# ── Case (b): selection picks one side of a recorded-but-unresolved mapping ─


def test_unresolved_mapping_requires_clarification(monkeypatch):
    _mock_evidence(monkeypatch, {
        "dbo.payments": (_evidence_row("concept-1", "FEE_SOURCE", "UNRESOLVED", evidence=("tuition fee",)),),
    })
    reason = ObjectSelectionReason(
        object_fqn="dbo.payments", supported_requirements=("total paid",), supporting_columns=("amount",),
    )
    candidate = _candidate(["dbo.payments"], [reason])
    result = check_ambiguity(
        "What is the total paid?", SOURCE_ID, USER_ID, candidate, _relationship_result(["dbo.payments"]),
        _schema_context([_context_document("dbo.payments")]), "fp-123",
    )
    assert result.is_ambiguous is True
    assert "tuition fee" in result.clarification_question


# ── Case (c): mutually exclusive verified mapping-role family conflict ────


def test_payment_family_conflict_requires_clarification(monkeypatch):
    _mock_evidence(monkeypatch, {
        "dbo.tuition_payments": (
            _evidence_row("concept-1", "FULL_PAYMENT_SOURCE", "VERIFIED", evidence=("SQL tuition payment",)),
        ),
        "dbo.iplan_installments": (
            _evidence_row("concept-2", "INSTALLMENT_PAYMENT_SOURCE", "VERIFIED", evidence=("IPLAN installment",)),
        ),
    })
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.tuition_payments", supported_requirements=("amount paid",), supporting_columns=("amount",),
        ),
        ObjectSelectionReason(
            object_fqn="dbo.iplan_installments", supported_requirements=("amount paid",), supporting_columns=("amount",),
        ),
    ]
    candidate = _candidate(["dbo.tuition_payments", "dbo.iplan_installments"], reasons)
    relationship_result = _relationship_result(["dbo.tuition_payments", "dbo.iplan_installments"])
    schema_context = _schema_context([
        _context_document("dbo.tuition_payments"), _context_document("dbo.iplan_installments"),
    ])

    result = check_ambiguity(
        "How much did students pay in total?", SOURCE_ID, USER_ID, candidate, relationship_result, schema_context,
        "fp-123",
    )
    assert result.is_ambiguous is True
    assert "SQL tuition payment" in result.clarification_question
    assert "IPLAN installment" in result.clarification_question


def test_payment_family_conflict_resolved_by_question_wording(monkeypatch):
    """The question already names one role's own recorded evidence phrase
    -- grammar/evidence resolves it, no clarification should be raised."""
    _mock_evidence(monkeypatch, {
        "dbo.tuition_payments": (
            _evidence_row("concept-1", "FULL_PAYMENT_SOURCE", "VERIFIED", evidence=("full tuition payment",)),
        ),
        "dbo.iplan_installments": (
            _evidence_row("concept-2", "INSTALLMENT_PAYMENT_SOURCE", "VERIFIED", evidence=("IPLAN installment",)),
        ),
    })
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.tuition_payments", supported_requirements=("amount paid",), supporting_columns=("amount",),
        ),
        ObjectSelectionReason(
            object_fqn="dbo.iplan_installments", supported_requirements=("amount paid",), supporting_columns=("amount",),
        ),
    ]
    candidate = _candidate(["dbo.tuition_payments", "dbo.iplan_installments"], reasons)
    relationship_result = _relationship_result(["dbo.tuition_payments", "dbo.iplan_installments"])
    schema_context = _schema_context([
        _context_document("dbo.tuition_payments"), _context_document("dbo.iplan_installments"),
    ])

    result = check_ambiguity(
        "How much full tuition payment did students pay?", SOURCE_ID, USER_ID, candidate, relationship_result,
        schema_context, "fp-123",
    )
    assert result.is_ambiguous is False


# ── Case (d): date-role conflict, scoped to cited columns only ────────────


def test_date_role_conflict_requires_clarification():
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.enrollments", supported_requirements=("recent enrollments",),
            supporting_columns=("StartDate", "InsertDate"),
        ),
    ]
    candidate = _candidate(["dbo.enrollments"], reasons)
    relationship_result = _relationship_result(["dbo.enrollments"])
    schema_context = _schema_context([
        _context_document("dbo.enrollments", date_columns=("StartDate", "InsertDate", "EndDate")),
    ])

    result = check_ambiguity(
        "Which students have the most recent activity?", SOURCE_ID, USER_ID, candidate, relationship_result,
        schema_context, "fp-123",
    )
    assert result.is_ambiguous is True
    assert "StartDate" in result.clarification_question
    assert "InsertDate" in result.clarification_question


def test_date_role_conflict_resolved_by_question_wording():
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.enrollments", supported_requirements=("enrollment start",),
            supporting_columns=("StartDate", "InsertDate"),
        ),
    ]
    candidate = _candidate(["dbo.enrollments"], reasons)
    relationship_result = _relationship_result(["dbo.enrollments"])
    schema_context = _schema_context([
        _context_document("dbo.enrollments", date_columns=("StartDate", "InsertDate")),
    ])

    result = check_ambiguity(
        "When did each student start their enrollment?", SOURCE_ID, USER_ID, candidate, relationship_result,
        schema_context, "fp-123",
    )
    assert result.is_ambiguous is False


def test_date_columns_not_cited_never_flagged():
    """Two conflicting date buckets exist on the table, but the selector
    never cited either as relevant to this question -- must not flag."""
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.enrollments", supported_requirements=("student count",), supporting_columns=("student_id",),
        ),
    ]
    candidate = _candidate(["dbo.enrollments"], reasons)
    relationship_result = _relationship_result(["dbo.enrollments"])
    schema_context = _schema_context([
        _context_document("dbo.enrollments", date_columns=("StartDate", "InsertDate")),
    ])

    result = check_ambiguity(
        "How many students are enrolled?", SOURCE_ID, USER_ID, candidate, relationship_result, schema_context,
        "fp-123",
    )
    assert result.is_ambiguous is False


# ── Case (e): shared descriptive attribute across multiple entities ───────


def test_shared_description_attribute_requires_clarification():
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.programs", supported_requirements=("description",), supporting_columns=("Description",),
        ),
        ObjectSelectionReason(
            object_fqn="dbo.classes", supported_requirements=("description",), supporting_columns=("Description",),
        ),
    ]
    candidate = _candidate(["dbo.programs", "dbo.classes"], reasons)
    relationship_result = _relationship_result(["dbo.programs", "dbo.classes"])
    schema_context = _schema_context([_context_document("dbo.programs"), _context_document("dbo.classes")])

    result = check_ambiguity(
        "What is the description?", SOURCE_ID, USER_ID, candidate, relationship_result, schema_context, "fp-123",
    )
    assert result.is_ambiguous is True


def test_shared_description_attribute_resolved_by_grammar():
    reasons = [
        ObjectSelectionReason(
            object_fqn="dbo.programs", supported_requirements=("description",), supporting_columns=("Description",),
        ),
        ObjectSelectionReason(
            object_fqn="dbo.classes", supported_requirements=("description",), supporting_columns=("Description",),
        ),
    ]
    candidate = _candidate(["dbo.programs", "dbo.classes"], reasons)
    relationship_result = _relationship_result(["dbo.programs", "dbo.classes"])
    schema_context = _schema_context([_context_document("dbo.programs"), _context_document("dbo.classes")])

    result = check_ambiguity(
        "What is the program description?", SOURCE_ID, USER_ID, candidate, relationship_result, schema_context,
        "fp-123",
    )
    assert result.is_ambiguous is False


# ── Literal values are never treated as schema ambiguity by this guard ────


def test_unfamiliar_literal_value_never_flagged(monkeypatch):
    monkeypatch.setattr(ambiguity_guard_module, "BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED", False)
    reason = ObjectSelectionReason(
        object_fqn="dbo.programs", supported_requirements=("program filter",), supporting_columns=("ProgramName",),
    )
    candidate = _candidate(["dbo.programs"], [reason])
    result = check_ambiguity(
        "How many students are in the Zzyzx Robotics program?", SOURCE_ID, USER_ID, candidate,
        _relationship_result(["dbo.programs"]), _schema_context([_context_document("dbo.programs")]), "fp-123",
    )
    assert result.is_ambiguous is False
