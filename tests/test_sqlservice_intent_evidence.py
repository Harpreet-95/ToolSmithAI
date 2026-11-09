"""
Focused unit tests — Phase 7, Step 3B, Part 3/5: the shared intent-evidence
data model (core/sqlservice/intent_evidence.py). Pure, no I/O, no database.

Run:
    venv/Scripts/pytest tests/test_sqlservice_intent_evidence.py -v
"""
from __future__ import annotations

import dataclasses

import pytest

from core.sqlservice.intent_evidence import (
    OPERATION_COUNT,
    OPERATION_GROUP,
    IntentEvidenceItem,
    IntentEvidenceStructureError,
    QuestionIntent,
    ReviewedIntentEvidence,
    material_requirements,
    parse_intent_evidence_items,
    parse_question_intent,
)


def _intent_dict(**overrides):
    value = {
        "operation": OPERATION_COUNT, "entities": ["student"], "measures": [],
        "filters": [], "date_constraints": [], "groupings": [],
    }
    value.update(overrides)
    return value


# ── parse_question_intent ───────────────────────────────────────────────────


def test_valid_intent_parses():
    intent = parse_question_intent(_intent_dict(operation=OPERATION_GROUP, groupings=["program"]))
    assert intent.operation == OPERATION_GROUP
    assert intent.entities == ("student",)
    assert intent.groupings == ("program",)


def test_intent_must_be_object():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent("not an object")


def test_intent_operation_must_be_known_value():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(operation="DELETE"))


def test_intent_missing_required_list_field_rejected():
    raw = _intent_dict()
    del raw["measures"]
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(raw)


def test_intent_list_field_wrong_type_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(entities="student"))


def test_intent_blank_entry_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(entities=["  "]))


def test_intent_duplicate_entry_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(entities=["student", "Student"]))


def test_intent_field_bound_enforced():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(entities=[f"e{i}" for i in range(50)]))


def test_intent_entry_character_bound_enforced():
    with pytest.raises(IntentEvidenceStructureError):
        parse_question_intent(_intent_dict(entities=["x" * 500]))


# ── parse_intent_evidence_items ─────────────────────────────────────────────


def test_valid_evidence_list_parses():
    items = parse_intent_evidence_items([
        {"requirement": "student", "object_fqn": "dbo.students", "column_name": None},
        {"requirement": "program", "object_fqn": "dbo.path", "column_name": "TrackName"},
    ])
    assert items[0] == IntentEvidenceItem("student", "dbo.students", None)
    assert items[1].column_name == "TrackName"


def test_evidence_must_be_a_list():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items({"requirement": "x"})


def test_evidence_bound_enforced():
    entries = [{"requirement": f"r{i}", "object_fqn": "dbo.t", "column_name": None} for i in range(100)]
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items(entries)


def test_evidence_blank_requirement_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "  ", "object_fqn": "dbo.t", "column_name": None}])


def test_evidence_blank_object_fqn_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "x", "object_fqn": "", "column_name": None}])


def test_evidence_star_column_name_rejected():
    """SELECT * can never substitute for explicit evidence."""
    with pytest.raises(IntentEvidenceStructureError, match="\\*"):
        parse_intent_evidence_items([{"requirement": "x", "object_fqn": "dbo.t", "column_name": "*"}])


def test_evidence_duplicate_entry_rejected():
    entry = {"requirement": "student", "object_fqn": "dbo.students", "column_name": None}
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([entry, dict(entry)])


def test_evidence_column_name_character_bound_enforced():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "x", "object_fqn": "dbo.t", "column_name": "c" * 500}])


def test_evidence_requirement_character_bound_enforced():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "r" * 500, "object_fqn": "dbo.t", "column_name": None}])


def test_evidence_non_string_column_name_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "x", "object_fqn": "dbo.t", "column_name": 5}])


def test_evidence_entry_missing_keys_rejected():
    with pytest.raises(IntentEvidenceStructureError):
        parse_intent_evidence_items([{"requirement": "x"}])


# ── material_requirements ───────────────────────────────────────────────────


def test_material_requirements_covers_every_category_except_operation():
    intent = QuestionIntent(
        operation=OPERATION_GROUP, entities=("student",), measures=("fee",),
        filters=("active",), date_constraints=("last year",), groupings=("program",),
    )
    reqs = material_requirements(intent)
    assert set(reqs) == {"student", "fee", "active", "last year", "program"}
    assert OPERATION_GROUP not in reqs


def test_material_requirements_empty_when_intent_has_none():
    intent = QuestionIntent(
        operation=OPERATION_COUNT, entities=(), measures=(), filters=(), date_constraints=(), groupings=(),
    )
    assert material_requirements(intent) == ()


# ── immutability ─────────────────────────────────────────────────────────


def test_dataclasses_are_frozen():
    intent = QuestionIntent("COUNT", (), (), (), (), ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.operation = "SUM"
    item = IntentEvidenceItem("r", "dbo.t", None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.object_fqn = "dbo.other"
    reviewed = ReviewedIntentEvidence(intent, ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        reviewed.intent = intent
