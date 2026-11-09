"""
Shared, dependency-free intent/evidence data model — Phase 7, Step 3B,
Part 3: the SQL reviewer's mandatory intent-evidence contract.

A single module with no imports from any other core.sqlservice.sql_* or
core.sqlservice.schema_* module (only core.config for its own bound
constants) — mirrors core.sqlservice.semantic_enrichment_phases' own
"shared vocabulary sits below every consumer" convention, so
the retired SQL-review stage (parses this from the reviewer's raw
JSON), core.sqlservice.sql_validation (validates it deterministically
against real schema/columns), and core.sqlservice.answer_generation_service
(consumes it for requirement-level answer grounding) can all import it
without any risk of a circular import between them.

This module performs only STRUCTURAL validation — required keys present,
correct types, bounded lengths/counts, no blank/duplicate entries. It has
no access to schema metadata and therefore cannot know whether a claimed
object_fqn/column_name is real; that deterministic check belongs entirely
to core.sqlservice.sql_validation (Phase 7, Step 3B, Part 3, requirement 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from core.config import (
    SQL_REVIEW_MAX_COLUMN_NAME_CHARACTERS,
    SQL_REVIEW_MAX_EVIDENCE_ITEMS,
    SQL_REVIEW_MAX_INTENT_DATE_CONSTRAINTS,
    SQL_REVIEW_MAX_INTENT_ENTITIES,
    SQL_REVIEW_MAX_INTENT_FILTERS,
    SQL_REVIEW_MAX_INTENT_GROUPINGS,
    SQL_REVIEW_MAX_INTENT_MEASURES,
    SQL_REVIEW_MAX_INTENT_TERM_CHARACTERS,
    SQL_REVIEW_MAX_REQUIREMENT_CHARACTERS,
)

__all__ = [
    "OPERATION_COUNT",
    "OPERATION_LIST",
    "OPERATION_SUM",
    "OPERATION_AVG",
    "OPERATION_GROUP",
    "OPERATION_TREND",
    "OPERATION_COMPARISON",
    "OPERATION_OTHER",
    "OPERATIONS",
    "QuestionIntent",
    "IntentEvidenceItem",
    "ReviewedIntentEvidence",
    "IntentEvidenceStructureError",
    "parse_question_intent",
    "parse_intent_evidence_items",
    "material_requirements",
]

OPERATION_COUNT = "COUNT"
OPERATION_LIST = "LIST"
OPERATION_SUM = "SUM"
OPERATION_AVG = "AVG"
OPERATION_GROUP = "GROUP"
OPERATION_TREND = "TREND"
OPERATION_COMPARISON = "COMPARISON"
OPERATION_OTHER = "OTHER"
OPERATIONS = frozenset({
    OPERATION_COUNT, OPERATION_LIST, OPERATION_SUM, OPERATION_AVG,
    OPERATION_GROUP, OPERATION_TREND, OPERATION_COMPARISON, OPERATION_OTHER,
})

_INTENT_LIST_FIELDS: tuple[tuple[str, int], ...] = (
    ("entities", SQL_REVIEW_MAX_INTENT_ENTITIES),
    ("measures", SQL_REVIEW_MAX_INTENT_MEASURES),
    ("filters", SQL_REVIEW_MAX_INTENT_FILTERS),
    ("date_constraints", SQL_REVIEW_MAX_INTENT_DATE_CONSTRAINTS),
    ("groupings", SQL_REVIEW_MAX_INTENT_GROUPINGS),
)


class IntentEvidenceStructureError(Exception):
    """The reviewer's `intent`/`evidence` payload failed structural
    validation (missing/wrong-typed key, blank entry, duplicate entry, or a
    bound exceeded) — never a schema-correctness error; see
    core.sqlservice.sql_validation for that deterministic check."""


@dataclass(frozen=True)
class QuestionIntent:
    """The reviewer's own structured statement of what the question asks
    for — never derived by a keyword-rule intent parser (requirement 1);
    the AI reviewer identifies this directly from the question."""

    operation: str
    entities: tuple[str, ...]
    measures: tuple[str, ...]
    filters: tuple[str, ...]
    date_constraints: tuple[str, ...]
    groupings: tuple[str, ...]


@dataclass(frozen=True)
class IntentEvidenceItem:
    """One requirement -> real-schema-object[.column] justification.
    `column_name` is None only when `requirement` is strictly an
    object/row-existence claim (see core.sqlservice.sql_validation's own
    deterministic enforcement of exactly when that is permitted)."""

    requirement: str
    object_fqn: str
    column_name: str | None


@dataclass(frozen=True)
class ReviewedIntentEvidence:
    """The complete, structurally-valid (not yet schema-validated) intent
    and evidence for one reviewed SQL candidate."""

    intent: QuestionIntent
    evidence: tuple[IntentEvidenceItem, ...]


def _require_str(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise IntentEvidenceStructureError(f"'{field}' must be a string")
    return value


def _require_bounded_string_list(value: object, field: str, max_items: int, max_characters: int) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise IntentEvidenceStructureError(f"'{field}' must be an array")
    if len(value) > max_items:
        raise IntentEvidenceStructureError(f"'{field}' exceeds the maximum of {max_items} entries")
    cleaned: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise IntentEvidenceStructureError(f"'{field}' entries must be strings")
        stripped = entry.strip()
        if not stripped:
            raise IntentEvidenceStructureError(f"'{field}' contains a blank entry")
        if len(stripped) > max_characters:
            raise IntentEvidenceStructureError(f"'{field}' entry exceeds {max_characters} characters")
        cleaned.append(stripped)
    lowered = [c.lower() for c in cleaned]
    if len(lowered) != len(set(lowered)):
        raise IntentEvidenceStructureError(f"'{field}' contains duplicate entries")
    return tuple(cleaned)


def parse_question_intent(raw: object) -> QuestionIntent:
    """Structurally validate and parse the reviewer's `intent` object.
    Raises IntentEvidenceStructureError for any shape violation."""
    if not isinstance(raw, Mapping):
        raise IntentEvidenceStructureError("'intent' must be a JSON object")

    operation = raw.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise IntentEvidenceStructureError(f"'intent.operation' must be one of {sorted(OPERATIONS)}")

    parsed: dict[str, tuple[str, ...]] = {}
    for field, max_items in _INTENT_LIST_FIELDS:
        if field not in raw:
            raise IntentEvidenceStructureError(f"'intent.{field}' is required")
        parsed[field] = _require_bounded_string_list(
            raw[field], f"intent.{field}", max_items, SQL_REVIEW_MAX_INTENT_TERM_CHARACTERS,
        )

    return QuestionIntent(
        operation=operation,
        entities=parsed["entities"], measures=parsed["measures"], filters=parsed["filters"],
        date_constraints=parsed["date_constraints"], groupings=parsed["groupings"],
    )


def parse_intent_evidence_items(raw: object) -> tuple[IntentEvidenceItem, ...]:
    """Structurally validate and parse the reviewer's `evidence` array.
    Raises IntentEvidenceStructureError for any shape violation, including
    a blank requirement/object_fqn, a bound exceeded, or an exact-duplicate
    (requirement, object_fqn, column_name) entry. Does NOT check that
    object_fqn/column_name are real — see core.sqlservice.sql_validation."""
    if not isinstance(raw, list):
        raise IntentEvidenceStructureError("'evidence' must be an array")
    if len(raw) > SQL_REVIEW_MAX_EVIDENCE_ITEMS:
        raise IntentEvidenceStructureError(f"'evidence' exceeds the maximum of {SQL_REVIEW_MAX_EVIDENCE_ITEMS} entries")

    items: list[IntentEvidenceItem] = []
    seen: set[tuple[str, str, str | None]] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise IntentEvidenceStructureError("'evidence' entries must be JSON objects")
        requirement = _require_str(entry.get("requirement"), "evidence[].requirement").strip()
        if not requirement:
            raise IntentEvidenceStructureError("'evidence[].requirement' must not be blank")
        if len(requirement) > SQL_REVIEW_MAX_REQUIREMENT_CHARACTERS:
            raise IntentEvidenceStructureError(
                f"'evidence[].requirement' exceeds {SQL_REVIEW_MAX_REQUIREMENT_CHARACTERS} characters"
            )
        object_fqn = _require_str(entry.get("object_fqn"), "evidence[].object_fqn").strip()
        if not object_fqn:
            raise IntentEvidenceStructureError("'evidence[].object_fqn' must not be blank")

        column_name_raw = entry.get("column_name")
        column_name: str | None
        if column_name_raw is None:
            column_name = None
        elif isinstance(column_name_raw, str):
            column_name = column_name_raw.strip()
            if not column_name:
                column_name = None
            elif column_name == "*":
                raise IntentEvidenceStructureError(
                    "'evidence[].column_name' must be a real column name, not '*' "
                    "(SELECT * can never substitute for explicit evidence)"
                )
            elif len(column_name) > SQL_REVIEW_MAX_COLUMN_NAME_CHARACTERS:
                raise IntentEvidenceStructureError(
                    f"'evidence[].column_name' exceeds {SQL_REVIEW_MAX_COLUMN_NAME_CHARACTERS} characters"
                )
        else:
            raise IntentEvidenceStructureError("'evidence[].column_name' must be a string or null")

        dedup_key = (requirement.lower(), object_fqn.lower(), column_name.lower() if column_name else None)
        if dedup_key in seen:
            raise IntentEvidenceStructureError(
                f"'evidence' contains a duplicate entry for requirement {requirement!r}"
            )
        seen.add(dedup_key)
        items.append(IntentEvidenceItem(requirement=requirement, object_fqn=object_fqn, column_name=column_name))

    return tuple(items)


def material_requirements(intent: QuestionIntent) -> tuple[str, ...]:
    """Every material requirement text the reviewer's own evidence must
    cover — one entry per non-blank entity/measure/filter/date_constraint/
    grouping the reviewer itself declared, in that fixed order, exactly as
    provided (already deduplicated/blank-checked by parse_question_intent).
    Never includes `operation` itself — a bare COUNT/LIST verb is not a
    "requirement" with its own supporting column; it's satisfied by the
    entity/object-existence evidence instead (see core.sqlservice.
    sql_validation's own column_name-optional rule for entity requirements)."""
    return intent.entities + intent.measures + intent.filters + intent.date_constraints + intent.groupings
