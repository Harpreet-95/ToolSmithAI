"""
Focused unit tests — SQL rebuild Phase 3, Step 2: strict SQL Server
read-only query validation (core/sqlservice/sql_validation.py).

No database of any kind is used in this file — validate_sql_candidate()
takes an already-built SqlGenerationResult + SchemaContextResult directly,
so these tests construct both in memory. No OpenAI call, no SQL
execution, no live SQL Server or SQLite of any kind.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_validation.py -v
"""

import dataclasses
import hashlib
import os
from types import MappingProxyType

import pytest
from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-phase1-testing!!")
os.environ.setdefault("USER_ID_SALT", "test-salt-for-phase1-testing")

from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_SEMANTIC,
    STATUS_INCOMPLETE as SCHEMA_STATUS_INCOMPLETE,
    STATUS_OK as SCHEMA_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.sql_generation_service import (
    STATUS_INVALID_CONTEXT as GEN_STATUS_INVALID_CONTEXT,
    STATUS_OK as GEN_STATUS_OK,
    SqlGenerationCandidate,
    SqlGenerationResult,
)
from core.sqlservice.sql_validation import (
    STATUS_INVALID_CANDIDATE,
    STATUS_OK,
    STATUS_REJECTED,
    SqlValidationResult,
    validate_sql_candidate,
)


# ── Fixture-free helpers (no database in this test file) ──────────────────


def _doc(fqn, *, tier=1, object_type="TABLE", columns=("id",), metadata_extra=None):
    text = f"OBJECT: {fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  id INTEGER [PK]"
    metadata = {"column_names": tuple(columns)}
    metadata.update(metadata_extra or {})
    return ContextDocument(
        object_fqn=fqn, object_type=object_type, retrieval_tier=tier,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType(metadata), score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _confirmed_edge(column, other_fqn, other_column, *, direction="outbound", kind="PHYSICAL_FK"):
    """One core.sqlservice.retrieval_documents-shaped confirmed_relationship_edges
    entry — the real metadata shape core.sqlservice.sql_validation's Phase 7,
    Step 3A join validator reads (never document_text prose)."""
    return {"direction": direction, "column": column, "other": f"{other_fqn}.{other_column}", "kind": kind}


def _context(documents, *, build_id=1, status=SCHEMA_STATUS_OK, schema_fingerprint="fp-123", error=None):
    return SchemaContextResult(
        status=status, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536,
        documents=tuple(documents), context_text="context", total_characters=7,
        truncated=False, warnings=(), error=error,
    )


def _generation_result(
    sql, referenced, *, build_id=1, schema_fingerprint="fp-123", status=GEN_STATUS_OK,
    needs_clarification=False, clarification_question=None,
):
    candidate = None
    if status == GEN_STATUS_OK:
        candidate = SqlGenerationCandidate(
            sql=sql, interpretation="An interpretation.", referenced_object_fqns=tuple(referenced),
            assumptions=(), needs_clarification=needs_clarification, clarification_question=clarification_question,
        )
    return SqlGenerationResult(
        status=status, candidate=candidate, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id="fake", model="fake-model", warnings=(), error=None,
    )


DEFAULT_CONTEXT = _context([
    _doc("dbo.students"),
    _doc(
        "dbo.enrollments", columns=("id", "course_id", "student_id"),
        metadata_extra={"confirmed_relationship_edges": (_confirmed_edge("student_id", "dbo.students", "id"),)},
    ),
    _doc("staging.students"),
])


# ── 1. Basic SELECT ─────────────────────────────────────────────────────────

def test_basic_select_accepted():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.students",)
    assert result.statement_type == "Select"
    assert result.sql == "SELECT id FROM dbo.students"
    assert result.error is None


# ── 2. CTE SELECT (valid, no write) ────────────────────────────────────────

def test_cte_select_accepted():
    sql = "WITH ranked AS (SELECT id FROM dbo.students) SELECT * FROM ranked"
    gen = _generation_result(sql, ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.students",)


# ── 3. UNION / UNION ALL ────────────────────────────────────────────────────

def test_union_all_accepted():
    sql = "SELECT id FROM dbo.students UNION ALL SELECT id FROM dbo.enrollments"
    gen = _generation_result(sql, ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.statement_type == "Union"
    assert result.canonical_referenced_object_fqns == ("dbo.enrollments", "dbo.students")


def test_except_and_intersect_accepted():
    context = _context([_doc("dbo.students")])
    for op, label in (("EXCEPT", "Except"), ("INTERSECT", "Intersect")):
        sql = f"SELECT id FROM dbo.students {op} SELECT id FROM dbo.students"
        gen = _generation_result(sql, ["dbo.students"])
        result = validate_sql_candidate(gen, context)
        assert result.status == STATUS_OK, result.error
        assert result.statement_type == label


# ── 4. Aliases and joins ────────────────────────────────────────────────────

def test_aliases_and_joins_accepted():
    sql = (
        "SELECT s.id, e.course_id FROM dbo.students AS s "
        "JOIN dbo.enrollments AS e ON e.student_id = s.id"
    )
    gen = _generation_result(sql, ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.enrollments", "dbo.students")


# ── 5. Schema-qualified references ─────────────────────────────────────────

def test_schema_qualified_reference_resolved_case_insensitively():
    gen = _generation_result("SELECT id FROM DBO.STUDENTS", ["DBO.STUDENTS"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.students",)  # canonical spelling, not the SQL's casing


# ── 6. Uniquely resolvable unqualified names ───────────────────────────────

def test_unique_unqualified_name_resolved():
    gen = _generation_result("SELECT id FROM enrollments", ["dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_OK
    assert result.canonical_referenced_object_fqns == ("dbo.enrollments",)


# ── 7. Ambiguous unqualified names ──────────────────────────────────────────

def test_ambiguous_unqualified_name_rejected():
    gen = _generation_result("SELECT id FROM students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)  # both dbo.students and staging.students exist

    assert result.status == STATUS_REJECTED
    assert "ambiguous" in result.error


# ── 8. Unknown tables ────────────────────────────────────────────────────────

def test_unknown_table_rejected():
    gen = _generation_result("SELECT id FROM dbo.ghost_table", ["dbo.ghost_table"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "not present in the supplied schema context" in result.error


# ── 9. Mismatch with candidate-declared references ─────────────────────────

def test_undeclared_sql_reference_rejected():
    gen = _generation_result("SELECT id FROM dbo.students", [])  # SQL uses students, declares nothing
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "not declared" in result.error


def test_declared_but_unused_reference_rejected():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "not used anywhere in the SQL" in result.error


# ── 10. Multiple statements ──────────────────────────────────────────────────

def test_multiple_statements_rejected():
    gen = _generation_result("SELECT 1; SELECT 2;", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "exactly one" in result.error


# ── 11. Every prohibited statement/category ────────────────────────────────

@pytest.mark.parametrize("sql", [
    "INSERT INTO dbo.students (id) VALUES (1)",
    "UPDATE dbo.students SET id = 1",
    "DELETE FROM dbo.students",
    "MERGE INTO dbo.students USING dbo.enrollments ON 1=1 WHEN MATCHED THEN UPDATE SET id = 1",
    "CREATE TABLE dbo.new_table (id INT)",
    "ALTER TABLE dbo.students ADD COLUMN x INT",
    "DROP TABLE dbo.students",
    "TRUNCATE TABLE dbo.students",
    "EXEC dbo.my_proc",
    "EXECUTE dbo.my_proc",
    "EXEC sp_who",
    "BEGIN TRAN",
    "COMMIT",
    "ROLLBACK",
    "SET NOCOUNT ON",
    "USE some_database",
    "DECLARE @x INT",
])
def test_prohibited_statement_categories_rejected(sql):
    gen = _generation_result(sql, [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED, f"expected rejection for: {sql}"


def test_select_into_temp_table_rejected():
    gen = _generation_result("SELECT * INTO #temp FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED


def test_temp_table_reference_rejected():
    gen = _generation_result("SELECT * FROM #temp", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED
    assert "temp table" in result.error


def test_table_variable_reference_rejected():
    gen = _generation_result("SELECT * FROM @MyTableVar", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED


@pytest.mark.parametrize("sql", [
    "SELECT * FROM OPENROWSET('SQLNCLI', 'Server=S;Trusted_Connection=yes;', 'SELECT * FROM x') AS t",
    "SELECT * FROM OPENQUERY(linkedserver, 'SELECT * FROM x')",
    "SELECT * FROM OPENDATASOURCE('SQLNCLI', 'x').db.dbo.students",
])
def test_openrowset_family_rejected(sql):
    gen = _generation_result(sql, [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED


def test_bulk_insert_rejected_as_unparseable():
    gen = _generation_result("BULK INSERT dbo.students FROM 'C:\\data.csv'", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED


# ── 12. Writes hidden inside CTEs ──────────────────────────────────────────

def test_delete_hidden_inside_cte_rejected():
    sql = "WITH x AS (DELETE FROM dbo.students OUTPUT deleted.* ) SELECT * FROM x"
    gen = _generation_result(sql, [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "Delete" in result.error


def test_insert_hidden_inside_cte_rejected():
    sql = "WITH x AS (INSERT INTO dbo.students (id) OUTPUT inserted.* VALUES (1)) SELECT * FROM x"
    gen = _generation_result(sql, [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED


# ── 13. Four-part / cross-database names ───────────────────────────────────

def test_cross_database_three_part_reference_rejected():
    gen = _generation_result("SELECT id FROM otherdb.dbo.students", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED
    assert "cross-database" in result.error


def test_linked_server_four_part_reference_rejected():
    gen = _generation_result("SELECT id FROM server1.otherdb.dbo.students", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_REJECTED


# ── 14. Comments and obfuscated keywords ───────────────────────────────────

def test_line_comment_rejected():
    gen = _generation_result("SELECT id FROM dbo.students -- comment", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED
    assert "comment" in result.error


def test_block_comment_rejected():
    gen = _generation_result("SELECT id FROM dbo.students /* comment */", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED


def test_obfuscated_keyword_via_comment_rejected():
    gen = _generation_result("SEL/**/ECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED  # rejected pre-parse for containing a comment marker


# ── 15. Parser errors ───────────────────────────────────────────────────────

def test_unparseable_garbage_rejected():
    gen = _generation_result("SELEC BAD GARBAGE ###", [])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_REJECTED
    assert "could not be parsed" in result.error


# ── 16. SQL / AST / CTE / join / object limits ─────────────────────────────

def test_sql_length_limit_enforced():
    long_sql = "SELECT id FROM dbo.students WHERE id IN (" + ",".join(str(i) for i in range(1000)) + ")"
    gen = _generation_result(long_sql, ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, max_sql_characters=50)

    assert result.status == STATUS_REJECTED
    assert "exceeds the maximum" in result.error


def test_ast_node_count_limit_enforced():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, max_ast_nodes=1)

    assert result.status == STATUS_REJECTED
    assert "AST node count" in result.error


def test_cte_count_limit_enforced():
    sql = (
        "WITH a AS (SELECT id FROM dbo.students), b AS (SELECT id FROM dbo.enrollments) "
        "SELECT * FROM a JOIN b ON 1=1"
    )
    gen = _generation_result(sql, ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, max_ctes=1)

    assert result.status == STATUS_REJECTED
    assert "CTE count" in result.error


def test_join_count_limit_enforced():
    sql = "SELECT * FROM dbo.students a JOIN dbo.enrollments b ON 1=1 JOIN dbo.enrollments c ON 1=1"
    gen = _generation_result(sql, ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, max_joins=1)

    assert result.status == STATUS_REJECTED
    assert "JOIN count" in result.error


def test_referenced_object_count_limit_enforced():
    sql = "SELECT * FROM dbo.students JOIN dbo.enrollments ON 1=1"
    gen = _generation_result(sql, ["dbo.students", "dbo.enrollments"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, max_referenced_objects=1)

    assert result.status == STATUS_REJECTED
    assert "referenced-object count" in result.error


def test_invalid_limit_configuration_raises():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    with pytest.raises(ValueError):
        validate_sql_candidate(gen, DEFAULT_CONTEXT, max_sql_characters=0)
    with pytest.raises(ValueError):
        validate_sql_candidate(gen, DEFAULT_CONTEXT, max_ast_nodes=-1)


# ── 17. Deterministic canonical output ─────────────────────────────────────

def test_deterministic_canonical_output_across_repeated_calls():
    gen = _generation_result("SELECT id FROM DBO.Students", ["DBO.Students"])
    first = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    second = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert first == second
    assert first.canonical_referenced_object_fqns == ("dbo.students",)  # real FQN casing preserved, never "DBO.Students"


# ── 18. Immutable results ───────────────────────────────────────────────────

def test_result_is_immutable():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "CHANGED"


def test_result_carries_no_secret_fields():
    field_names = set(SqlValidationResult.__dataclass_fields__.keys())
    assert not any(
        "key" in name.lower() or "secret" in name.lower() or "token" in name.lower() or "password" in name.lower()
        for name in field_names
    )


# ── Invalid-candidate / precondition short-circuits ────────────────────────

def test_generation_result_not_ok_is_invalid_candidate():
    gen = _generation_result("", [], status=GEN_STATUS_INVALID_CONTEXT)
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_INVALID_CANDIDATE


def test_needs_clarification_is_invalid_candidate():
    gen = _generation_result(
        "", [], needs_clarification=True, clarification_question="Which period?",
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)

    assert result.status == STATUS_INVALID_CANDIDATE


def test_unsuccessful_schema_context_is_invalid_candidate():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    bad_context = _context([], status=SCHEMA_STATUS_INCOMPLETE, error="incomplete embeddings")
    result = validate_sql_candidate(gen, bad_context)

    assert result.status == STATUS_INVALID_CANDIDATE


def test_mismatched_build_id_raises():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"], build_id=1)
    other_context = _context([_doc("dbo.students")], build_id=2)

    with pytest.raises(ValueError):
        validate_sql_candidate(gen, other_context)


# ── Phase 7, Step 3B, Part 3 — deterministic intent-evidence validation ────

from core.sqlservice.intent_evidence import IntentEvidenceItem, QuestionIntent, ReviewedIntentEvidence


def _intent_evidence(evidence, *, entities=("student",), measures=(), filters=(), groupings=()):
    intent = QuestionIntent(
        operation="LIST", entities=entities, measures=measures, filters=filters,
        date_constraints=(), groupings=groupings,
    )
    return ReviewedIntentEvidence(intent=intent, evidence=tuple(evidence))


def test_valid_entity_evidence_accepted_without_column():
    """Row-count/existence requirement works without inventing a measure column."""
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence([IntentEvidenceItem("student", "dbo.students", None)])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_OK


def test_valid_column_evidence_accepted():
    gen = _generation_result(
        "SELECT student_id, course_id FROM dbo.enrollments", ["dbo.enrollments"],
    )
    evidence = _intent_evidence(
        [
            IntentEvidenceItem("student", "dbo.enrollments", "student_id"),
            IntentEvidenceItem("course", "dbo.enrollments", "course_id"),
        ],
        entities=("student", "course"),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_OK


def test_nonexistent_evidence_column_rejected():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence(
        [IntentEvidenceItem("enrollment date", "dbo.students", "EnrollmentDate")],
        entities=(), filters=("enrollment date",),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


def test_evidence_from_unused_object_rejected():
    """dbo.enrollments is a real object in the schema context, but this
    SQL never uses it — evidence pointing there must still be rejected."""
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence(
        [IntentEvidenceItem("course", "dbo.enrollments", "course_id")],
        entities=(), filters=("course",),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "does not actually use" in result.error


def test_evidence_object_not_in_schema_context_rejected():
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence(
        [IntentEvidenceItem("widget", "dbo.widgets", None)], entities=("widget",),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "not present in the supplied schema context" in result.error


def test_measure_requirement_without_column_rejected():
    """A measure/filter/date/grouping requirement may never omit a real
    column — only a bare entity requirement can."""
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence(
        [IntentEvidenceItem("total fees", "dbo.students", None)],
        entities=(), measures=("total fees",),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "must name a real supporting column" in result.error


def test_material_requirement_with_no_evidence_at_all_rejected():
    """The reviewer's own evidence list omits a requirement entirely —
    sql_review_service should already have caught this, but sql_validation
    is the FINAL authority and must reject independently too."""
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    evidence = _intent_evidence([], entities=("student",))
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "no valid supporting evidence" in result.error


def test_no_intent_evidence_parameter_preserves_prior_behavior():
    """intent_evidence=None (the default) must validate exactly as before
    Part 3 existed."""
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT)
    assert result.status == STATUS_OK


def test_arbitrary_object_with_no_relevant_column_cannot_be_approved():
    """An object genuinely present and used by the SQL, but with no column
    that actually supports the claimed requirement, is still rejected."""
    gen = _generation_result(
        "SELECT s.id FROM dbo.students s JOIN dbo.enrollments e ON e.student_id = s.id",
        ["dbo.students", "dbo.enrollments"],
    )
    evidence = _intent_evidence(
        [
            IntentEvidenceItem("student", "dbo.students", None),
            IntentEvidenceItem("graduation date", "dbo.enrollments", "GraduationDate"),
        ],
        entities=("student",), filters=("graduation date",),
    )
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, intent_evidence=evidence)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── Phase 7, Step 6E1 — Tier 3/4 governance re-check (final authority,
#    independent of whatever admitted the object earlier in the pipeline) ──

import core.sqlservice.sql_validation as sql_validation_module
from core.sqlservice.business_knowledge_evidence import (
    BusinessKnowledgeEvidenceIdentity,
    BusinessKnowledgeEvidenceResult,
    BusinessKnowledgeMappingEvidence,
)

_TIER34_CONTEXT = _context([
    _doc("dbo.students", columns=("id", "user_id")),
    _doc(
        "framework.users", tier=3, columns=("UserID", "FirstName", "LastName"),
        metadata_extra={
            "primary_key_columns": ("UserID",),
            "confirmed_relationship_edges": (_confirmed_edge("UserID", "dbo.students", "user_id", direction="inbound"),),
        },
    ),
])


def _evidence_result(mappings_by_fqn_lower):
    return BusinessKnowledgeEvidenceResult(
        identity=BusinessKnowledgeEvidenceIdentity(package_id=1, schema_fingerprint="fp-123"),
        mappings_by_object_fqn_lower=mappings_by_fqn_lower,
    )


def _verified_mapping(*, columns=()):
    return BusinessKnowledgeMappingEvidence(
        concept_id="STUDENT_NAME", concept_type="ENTITY", concept_origin="AUTHORITATIVE",
        mapping_role="DETAIL_ACTIVITY_SOURCE", mapping_status="VERIFIED", confidence=0.9,
        evidence=("owner-confirmed",), columns=tuple(columns),
    )


def test_source_id_omitted_skips_governance_check_even_for_tier34(monkeypatch):
    """source_id=None/user_id=None (the default) must validate a tier-3/4
    object exactly as every existing caller/test already does -- zero
    behavior change unless a caller opts in."""
    def _forbidden(*a, **k):
        raise AssertionError("load_business_knowledge_evidence must not be called when source_id/user_id are None")
    monkeypatch.setattr(sql_validation_module, "load_business_knowledge_evidence", _forbidden)

    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT)
    assert result.status == STATUS_OK


def test_tier34_object_without_verified_mapping_rejected(monkeypatch):
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({}),
    )
    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_REJECTED
    assert "no VERIFIED business-knowledge governance mapping" in result.error


def test_tier34_object_with_verified_mapping_and_approved_column_passes(monkeypatch):
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({"framework.users": (_verified_mapping(columns=["FirstName"]),)}),
    )
    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_OK


def test_tier34_object_with_verified_mapping_but_unapproved_column_rejected(monkeypatch):
    """FirstName is approved; LastName is not -- referencing LastName must
    reject the whole candidate even though FirstName alone would pass."""
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({"framework.users": (_verified_mapping(columns=["FirstName"]),)}),
    )
    gen = _generation_result(
        "SELECT u.FirstName, u.LastName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_REJECTED
    assert "not in its approved" in result.error
    assert "lastname" in result.error.lower()


def test_tier34_object_join_column_always_allowed_even_when_not_approved(monkeypatch):
    """The object's own primary key / confirmed join column is structural
    identity, never a selectable business attribute -- it must remain
    usable for the JOIN even when the mapping's approved-column list names
    only an unrelated attribute column."""
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({"framework.users": (_verified_mapping(columns=["FirstName"]),)}),
    )
    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_OK  # UserID (the join column) never rejected


def test_tier34_object_with_mapping_naming_no_columns_rejects_every_attribute(monkeypatch):
    """A VERIFIED mapping that names no specific columns approves the
    object's structural join/identity columns ONLY -- referencing any
    other column must still be rejected, never silently allow-all."""
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({"framework.users": (_verified_mapping(columns=()),)}),
    )
    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"],
    )
    result = validate_sql_candidate(gen, _TIER34_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_REJECTED
    assert "not in its approved" in result.error


def test_tier34_object_missing_schema_fingerprint_rejected_fail_closed(monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError("must fail closed before ever consulting evidence storage")
    monkeypatch.setattr(sql_validation_module, "load_business_knowledge_evidence", _forbidden)

    context_no_fingerprint = dataclasses.replace(_TIER34_CONTEXT, schema_fingerprint=None)
    gen = _generation_result(
        "SELECT u.FirstName FROM dbo.students s JOIN framework.users u ON u.UserID = s.user_id",
        ["dbo.students", "framework.users"], schema_fingerprint=None,
    )
    result = validate_sql_candidate(gen, context_no_fingerprint, source_id=90004, user_id="28")
    assert result.status == STATUS_REJECTED
    assert "no schema_fingerprint is available" in result.error


def test_tier12_object_never_triggers_governance_check(monkeypatch):
    """Only tier 3/4 objects are re-checked -- an ordinary tier-1 object
    must validate exactly as before even when source_id/user_id are given
    and no business-knowledge evidence exists at all."""
    monkeypatch.setattr(
        sql_validation_module, "load_business_knowledge_evidence",
        lambda *a, **k: _evidence_result({}),
    )
    gen = _generation_result("SELECT id FROM dbo.students", ["dbo.students"])
    result = validate_sql_candidate(gen, DEFAULT_CONTEXT, source_id=90004, user_id="28")
    assert result.status == STATUS_OK
