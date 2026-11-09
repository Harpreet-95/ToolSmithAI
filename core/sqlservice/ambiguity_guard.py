"""
Phase 7, Step 4, Part 2 -- generic pre-generation ambiguity guard for the
AI-first pipeline (core.sqlservice.ai_first_result).

Runs once, after schema_relationship_completion.complete_relationships()
and schema_exact_context.build_exact_schema_context() have both succeeded,
immediately before generate_sql_candidate() -- pure/local: no AI call, no
new schema probing, no new semantic-enrichment run. It reasons only over
data the pipeline has ALREADY loaded by that point:

  - SchemaSelectionCandidate.intent_summary / object_reasons (the AI
    selector's own stated reasoning -- which columns it actually cited as
    relevant to this question, never every column a selected object
    happens to have);
  - the schema_context documents' own already-computed `date_columns`
    metadata (core.sqlservice.retrieval_documents, populated when the
    document was originally built -- never a new column probe here);
  - VERIFIED/UNRESOLVED business-knowledge mapping evidence
    (core.sqlservice.business_knowledge_evidence.
    load_business_knowledge_evidence), which itself already degrades to
    None ("no evidence") for a source with no active business-knowledge
    package or when core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED is
    off -- every check here treats that None exactly like "no signal",
    never an error.

Every rule below is driven by the generic, package-agnostic MAPPING_ROLES
taxonomy (data.business_knowledge_mapping_store) or by generic English
lexical patterns (date-role keywords, descriptive-column-name keywords)
applicable to any schema -- never a hardcoded question phrase and never a
specific table name from any one customer's schema. A question that
already names the disambiguating term (checked against the SAME structured
evidence/column names, not a fixed phrase list) is treated as resolved by
grammar and is NOT sent to clarification -- see each check's own "escape
hatch" comment. This mirrors Part 2's own requirement that grammar and
business evidence should resolve an attribute/date question whenever they
genuinely can, and that a clarification is reserved for real, remaining
ambiguity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from core.config import BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED
from core.sqlservice.business_knowledge_evidence import (
    BusinessKnowledgeEvidenceResult,
    BusinessKnowledgeMappingEvidence,
    load_business_knowledge_evidence,
)
from core.sqlservice.schema_context_retrieval import SchemaContextResult
from core.sqlservice.schema_relationship_completion import RelationshipCompletionResult
from core.sqlservice.schema_selection_service import SchemaSelectionCandidate
from data.business_knowledge_mapping_store import MAPPING_ROLES

__all__ = ["AmbiguityGuardResult", "check_ambiguity"]

_STATUS_VERIFIED = "VERIFIED"
_STATUS_UNRESOLVED = "UNRESOLVED"

# Mutually exclusive role families -- a question answerable from ONE role in
# a family is not automatically answerable from another role in the SAME
# family without changing the business meaning of the result (Part 2's own
# SQL-tuition-vs-IPLAN-installment example: FULL_PAYMENT_SOURCE vs
# INSTALLMENT_PAYMENT_SOURCE). Every member is a generic MAPPING_ROLES value
# already defined by the business-knowledge package system -- never a
# customer-specific table name, never specific to one package.
_PAYMENT_FAMILY = frozenset({
    "FEE_SOURCE", "FULL_PAYMENT_SOURCE", "INSTALLMENT_PAYMENT_SOURCE",
    "FEE_DEFINITION_SOURCE", "FEE_PAYMENT_SOURCE",
})
_ENTITY_FAMILY = frozenset({"CANONICAL_ENTITY_SOURCE", "PREFERRED_BUSINESS_VIEW", "DETAIL_ACTIVITY_SOURCE"})
_STATUS_FAMILY = frozenset({"STATUS_SOURCE", "OUTCOME_SOURCE"})
_ROLE_FAMILIES = (_PAYMENT_FAMILY, _ENTITY_FAMILY, _STATUS_FAMILY)
assert all(role in MAPPING_ROLES for family in _ROLE_FAMILIES for role in family), (
    "ambiguity_guard role family references a role no longer in MAPPING_ROLES"
)

# Generic (schema-agnostic) date-role lexical buckets -- column-NAME pattern
# matching only, never a table name. Applied only to columns the AI schema
# selector itself already cited as relevant (object_reasons[*].
# supporting_columns), never to every date column a table happens to have.
_DATE_BUCKET_KEYWORDS: Mapping[str, tuple[str, ...]] = {
    "start_or_effective": ("start", "begin", "effective", "enroll", "signup", "sign_up", "join"),
    "end_or_termination": ("end", "close", "expir", "terminat", "complet", "graduat", "exit", "withdraw"),
    "system_recorded":    ("insert", "creat", "added", "logged", "record", "entry"),
    "modified":           ("updat", "modif", "changed", "revised"),
}

# Generic descriptive-text column-name keywords -- used only to notice that
# TWO DIFFERENT selected objects each expose their own descriptive/free-text
# field that the selector cited as relevant (e.g. a "program" object and a
# "class" object both having their own description column) -- never a fixed
# table name.
_DESCRIPTIVE_COLUMN_KEYWORDS = ("description", "desc", "notes", "summary", "remarks", "title")

_MAX_CLARIFICATION_CHOICES = 4


@dataclass(frozen=True)
class AmbiguityGuardResult:
    """Result of one check_ambiguity() call. `warnings` is always a tuple,
    even when empty, matching every other stage result in this pipeline."""

    is_ambiguous: bool
    clarification_question: Optional[str]
    warnings: tuple


def _question_mentions(question_lower: str, phrase: str) -> bool:
    return bool(phrase) and phrase.lower() in question_lower


def _join_choices(choices: Sequence[str]) -> str:
    choices = list(choices)[:_MAX_CLARIFICATION_CHOICES]
    if len(choices) <= 1:
        return choices[0] if choices else ""
    if len(choices) == 2:
        return f"{choices[0]} or {choices[1]}"
    return ", ".join(choices[:-1]) + f", or {choices[-1]}"


def _mapping_rows_for_objects(
    evidence: Optional[BusinessKnowledgeEvidenceResult], object_fqns: Sequence[str],
) -> dict:
    if evidence is None:
        return {}
    rows_by_object: dict = {}
    for fqn in object_fqns:
        rows = evidence.mappings_by_object_fqn_lower.get(fqn.lower())
        if rows:
            rows_by_object[fqn] = rows
    return rows_by_object


def _choice_label(row: BusinessKnowledgeMappingEvidence) -> str:
    if row.evidence:
        return row.evidence[0]
    return row.mapping_role.replace("_", " ").title()


def _check_unresolved_concept(rows_by_object: Mapping[str, tuple]) -> Optional[str]:
    """Case (b): the current selection includes an object whose evidence
    for some concept is still UNRESOLVED -- a recorded-but-unsettled
    ambiguity the business-knowledge mapper itself could not confidently
    pick a side of. Answering from that object silently would silently
    pick one side of that same still-open question."""
    for rows in rows_by_object.values():
        for row in rows:
            if row.mapping_status == _STATUS_UNRESOLVED:
                label = _choice_label(row)
                return (
                    "This question could be answered from more than one recorded source, and it has "
                    f"not yet been confirmed which one is correct (for example: {label}). Could you "
                    "confirm which one you mean?"
                )
    return None


def _check_family_conflict(rows_by_object: Mapping[str, tuple], question_lower: str) -> Optional[str]:
    """Case (c): a measure/entity shared by materially different business
    families (e.g. a payment amount that could come from a full-payment
    source or an installment-payment source) without the question naming
    which family it means."""
    for family in _ROLE_FAMILIES:
        seen_roles: dict = {}
        for rows in rows_by_object.values():
            for row in rows:
                if row.mapping_status != _STATUS_VERIFIED or row.mapping_role not in family:
                    continue
                seen_roles.setdefault(row.mapping_role, row)
        if len(seen_roles) < 2:
            continue

        # Grammar/evidence escape hatch: if the question already names a
        # term unique to exactly one role's own recorded evidence, that
        # role is what was asked for -- proceed, do not manufacture a
        # clarification the evidence itself already resolved.
        matched_roles = [
            role for role, row in seen_roles.items()
            if any(_question_mentions(question_lower, phrase) for phrase in row.evidence)
        ]
        if len(matched_roles) == 1:
            continue

        choices = [_choice_label(row) for row in seen_roles.values()]
        return f"This could mean {_join_choices(choices)} -- which one did you mean?"
    return None


def _bucket_for_column(column_name: str) -> Optional[str]:
    name_lower = column_name.lower()
    for bucket, keywords in _DATE_BUCKET_KEYWORDS.items():
        if any(keyword in name_lower for keyword in keywords):
            return bucket
    return None


def _check_date_role_conflict(
    schema_context: SchemaContextResult, selection_candidate: SchemaSelectionCandidate, question_lower: str,
) -> Optional[str]:
    """Case (d): the requested date role has multiple materially different
    verified interpretations and no default is established. Scoped to
    columns the AI selector itself already cited as relevant to this
    question -- never every date column a selected object happens to
    have."""
    cited_columns = set()
    for reason in selection_candidate.object_reasons:
        cited_columns.update(reason.supporting_columns)
    if not cited_columns:
        return None

    buckets: dict = {}
    for doc in schema_context.documents:
        date_columns = doc.metadata.get("date_columns") or ()
        for column_name in date_columns:
            if column_name not in cited_columns:
                continue
            bucket = _bucket_for_column(column_name)
            if bucket is None:
                continue
            buckets.setdefault(bucket, []).append(f"{doc.object_fqn}.{column_name}")

    if len(buckets) < 2:
        return None

    # Grammar escape hatch: the question already names a keyword unique to
    # one bucket (e.g. "enrolled" vs "created") -- that bucket is what was
    # asked for.
    matched_buckets = [
        bucket for bucket, keywords in _DATE_BUCKET_KEYWORDS.items()
        if bucket in buckets and any(_question_mentions(question_lower, kw) for kw in keywords)
    ]
    if len(matched_buckets) == 1:
        return None

    choices = [columns[0] for columns in buckets.values()]
    return f"This question could use more than one date field ({_join_choices(choices)}) -- which date did you mean?"


def _check_shared_attribute_conflict(
    schema_context: SchemaContextResult, selection_candidate: SchemaSelectionCandidate, question_lower: str,
) -> Optional[str]:
    """Case (e): the requested description/attribute could belong to
    multiple selected entities and the intended entity cannot be resolved
    from grammar. Fires only when the AI selector cited a descriptive
    column as relevant on TWO OR MORE different selected objects -- a
    single object citing its own description column is never flagged."""
    doc_by_object = {doc.object_fqn: doc for doc in schema_context.documents}
    descriptive_by_object: dict = {}
    for reason in selection_candidate.object_reasons:
        for column_name in reason.supporting_columns:
            name_lower = column_name.lower()
            if any(keyword in name_lower for keyword in _DESCRIPTIVE_COLUMN_KEYWORDS):
                descriptive_by_object.setdefault(reason.object_fqn, column_name)
                break

    if len(descriptive_by_object) < 2:
        return None

    # Grammar escape hatch: the question already names the object's own
    # real table/view name (or a simple singular/plural variant) -- that is
    # the entity the attribute was asked about.
    def _object_is_named_in_question(object_fqn: str) -> bool:
        object_name = object_fqn.rsplit(".", 1)[-1].lower()
        tokens = [t for t in object_name.replace("_", " ").split() if len(t) > 2]
        return any(
            _question_mentions(question_lower, token) or _question_mentions(question_lower, token.rstrip("s"))
            for token in tokens
        )

    matched_objects = [fqn for fqn in descriptive_by_object if _object_is_named_in_question(fqn)]
    if len(matched_objects) == 1:
        return None

    choices = [f"{fqn}.{col}" for fqn, col in descriptive_by_object.items()]
    return f"This description could come from {_join_choices(choices)} -- which one did you mean?"


def check_ambiguity(
    question: str,
    source_id: int,
    user_id: str,
    selection_candidate: SchemaSelectionCandidate,
    relationship_result: RelationshipCompletionResult,
    schema_context: SchemaContextResult,
    schema_fingerprint: Optional[str],
) -> AmbiguityGuardResult:
    """
    Run every generic ambiguity check, in a fixed order, and stop at the
    first genuine ambiguity found -- one concise clarification question per
    call, never a stacked list of every issue found. Checks (b)/(c) need
    business-knowledge evidence, which itself already returns None (treated
    as "no signal") for a source with no active package or when
    core.config.BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED is off -- this function
    never raises or special-cases that; it simply has fewer signals to
    reason over.

    Never calls an AI provider, never issues a new schema probe, never
    reads/writes the database beyond the same business-knowledge lookup
    core.sqlservice.schema_frontier already performs earlier in this same
    pipeline for the identical (source_id, user_id, schema_fingerprint).
    """
    question_lower = (question or "").lower()

    # Same rollout gate core.sqlservice.schema_frontier already applies to
    # this identical call -- zero behavior change (and zero extra DB read)
    # for a source with the flag off, exactly like every other caller of
    # load_business_knowledge_evidence.
    evidence = (
        load_business_knowledge_evidence(source_id, user_id, schema_fingerprint)
        if BUSINESS_KNOWLEDGE_EVIDENCE_ENABLED else None
    )
    rows_by_object = _mapping_rows_for_objects(evidence, selection_candidate.selected_object_fqns)

    clarification = _check_unresolved_concept(rows_by_object)
    if clarification is None:
        clarification = _check_family_conflict(rows_by_object, question_lower)
    if clarification is None:
        clarification = _check_date_role_conflict(schema_context, selection_candidate, question_lower)
    if clarification is None:
        clarification = _check_shared_attribute_conflict(schema_context, selection_candidate, question_lower)

    if clarification is not None:
        return AmbiguityGuardResult(is_ambiguous=True, clarification_question=clarification, warnings=())
    return AmbiguityGuardResult(is_ambiguous=False, clarification_question=None, warnings=())
