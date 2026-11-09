"""
Focused tests — SQL rebuild Phase 5, Steps 9A/9B: trust-hardened
SQL-example candidate assembly (core/sqlservice/sql_example_candidate.py).

Pure/offline: no database, no OpenAI call, no live SQL Server — every
SqlValidationResult/SqlExecutionResult evidence artifact is constructed by
hand in memory.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_example_candidate.py -v
"""

from __future__ import annotations

import dataclasses

import pytest

from core.sqlservice.sql_example_candidate import (
    ExampleCandidateRejected,
    TrustedSqlExampleCandidate,
    build_trusted_example_candidate,
    verify_execution_consistency,
)
from core.sqlservice.sql_example_model import (
    PROVENANCE_DEVELOPER_VERIFICATION_SET,
    PROVENANCE_OBSERVED_UNVERIFIED,
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
    PROVENANCE_VALIDATED_EXECUTION,
    RESULT_SHAPE_COUNT,
    TRUST_LEVEL_TRUSTED,
    TRUST_LEVEL_UNTRUSTED,
)
from core.sqlservice.sql_execution import STATUS_ERROR as EXEC_STATUS_ERROR
from core.sqlservice.sql_execution import STATUS_OK as EXEC_STATUS_OK
from core.sqlservice.sql_execution import SqlExecutionResult
from core.sqlservice.sql_validation import STATUS_INVALID_CANDIDATE, STATUS_OK as VALIDATION_STATUS_OK
from core.sqlservice.sql_validation import STATUS_REJECTED as VALIDATION_STATUS_REJECTED
from core.sqlservice.sql_validation import SqlValidationResult, ValidatedColumnReference


def _validation_result(
    sql="SELECT COUNT(*) FROM dbo.Student WHERE Status = 'Active'",
    *, status=VALIDATION_STATUS_OK, build_id=1, schema_fingerprint="fp-1",
    canonical_referenced_object_fqns=("dbo.Student",), error=None,
    validated_column_references=(),
):
    return SqlValidationResult(
        status=status, sql=sql, canonical_referenced_object_fqns=canonical_referenced_object_fqns,
        statement_type="Select" if status == VALIDATION_STATUS_OK else None,
        build_id=build_id, schema_fingerprint=schema_fingerprint, warnings=(), error=error,
        validated_column_references=validated_column_references,
    )


def _execution_result(
    *, status=EXEC_STATUS_OK, build_id=1, columns=("n",), rows=((5,),), row_count=1, error=None, error_category=None,
):
    return SqlExecutionResult(
        status=status, source_id=1, build_id=build_id, schema_fingerprint="fp-1",
        canonical_referenced_object_fqns=("dbo.Student",), columns=columns, rows=rows, row_count=row_count,
        truncated=False, truncation_reason=None, duration_ms=5, warnings=(), error=error, error_category=error_category,
    )


# ── Gate: validation_result must be real and OK ────────────────────────────

def test_rejects_non_ok_validation_result():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            validation_result=_validation_result(status=VALIDATION_STATUS_REJECTED, error="bad sql"),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_rejects_invalid_candidate_validation_result():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET,
            validation_result=_validation_result(status=STATUS_INVALID_CANDIDATE),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_rejects_non_sqlvalidationresult_object():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            validation_result={"status": "OK"},  # not a real SqlValidationResult
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_rejects_unrecognized_provenance():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance="SOMETHING_ELSE",
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_observed_unverified_provenance_rejected_up_front():
    """Step 9B: build_trusted_example_candidate() never returns an
    untrusted candidate at all -- PROVENANCE_OBSERVED_UNVERIFIED (which
    can never classify as TRUSTED) is rejected immediately, before
    validation_result is even consulted. Use core.sqlservice.
    sql_example_store.insert_untrusted_observation() to record one."""
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_OBSERVED_UNVERIFIED,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


# ── Safe normalization is mandatory ─────────────────────────────────────────

def test_normalization_failure_rejects_candidate():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            validation_result=_validation_result(sql="SELECT * FROM dbo.T; DROP TABLE dbo.T"),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_sql_template_is_the_normalized_form_never_the_raw_sql():
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(),
        question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert "'Active'" not in candidate.sql_template
    assert ":string_literal" in candidate.sql_template


def test_canonical_object_fqns_taken_from_validation_result_not_recomputed():
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(canonical_referenced_object_fqns=("dbo.Student", "dbo.Class")),
        question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_object_fqns == ("dbo.Student", "dbo.Class")


# ── SCHEMA_GENERATED_TEMPLATE / DEVELOPER_VERIFICATION_SET: gates 1-4 only ─

def test_schema_generated_template_trusted_once_validated_and_normalized():
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(),
        question_pattern="How many :entity", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.trust_level == TRUST_LEVEL_TRUSTED
    assert isinstance(candidate, TrustedSqlExampleCandidate)


def test_developer_verification_set_still_requires_validation_to_pass():
    """The exact Step 9A requirement: no provenance label alone may bypass
    validation, including DEVELOPER_VERIFICATION_SET."""
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET,
            validation_result=_validation_result(status=VALIDATION_STATUS_REJECTED, error="ambiguous table"),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


def test_developer_verification_set_trusted_once_validated_and_normalized():
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_DEVELOPER_VERIFICATION_SET,
        validation_result=_validation_result(),
        question_pattern="How many :entity", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.trust_level == TRUST_LEVEL_TRUSTED


# ── VALIDATED_EXECUTION: requires real, independent execution evidence ────

def test_validated_execution_requires_execution_result():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            repeat_execution_result=_execution_result(),
        )


def test_validated_execution_requires_repeat_execution_result():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            execution_result=_execution_result(),
        )


def test_validated_execution_rejects_non_sqlexecutionresult_evidence():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            execution_result={"status": "OK"}, repeat_execution_result=_execution_result(),
        )


def test_validated_execution_trusted_when_both_executions_agree():
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
        validation_result=_validation_result(),
        question_pattern="How many :entity", result_shape=RESULT_SHAPE_COUNT,
        execution_result=_execution_result(), repeat_execution_result=_execution_result(),
    )
    assert candidate.trust_level == TRUST_LEVEL_TRUSTED


def test_validated_execution_rejected_when_executions_disagree():
    """The exact Step 9 requirement: execution success alone is never
    sufficient -- two independent runs that disagree must not be trusted.
    Step 9B tightening: this is now a rejection, not an untrusted
    candidate -- build_trusted_example_candidate() never returns one."""
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            execution_result=_execution_result(rows=((5,),)),
            repeat_execution_result=_execution_result(rows=((7,),)),
        )


def test_validated_execution_rejected_when_second_execution_errored():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            execution_result=_execution_result(status=EXEC_STATUS_OK),
            repeat_execution_result=_execution_result(status=EXEC_STATUS_ERROR, error="timeout", error_category="timeout"),
        )


def test_validated_execution_rejects_mismatched_build_id():
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_VALIDATED_EXECUTION,
            validation_result=_validation_result(build_id=1),
            question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
            execution_result=_execution_result(build_id=1), repeat_execution_result=_execution_result(build_id=2),
        )


def test_schema_generated_template_ignores_execution_evidence_if_given():
    """Passing execution evidence for a provenance that doesn't need it is
    harmless -- never an error, never consulted."""
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(),
        question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        execution_result=_execution_result(status=EXEC_STATUS_ERROR),
    )
    assert candidate.trust_level == TRUST_LEVEL_TRUSTED


# ── verify_execution_consistency: pure comparator ──────────────────────────

def test_verify_execution_consistency_true_for_identical_successful_runs():
    assert verify_execution_consistency(_execution_result(), _execution_result()) is True


def test_verify_execution_consistency_false_for_differing_rows():
    assert verify_execution_consistency(
        _execution_result(rows=((1,),)), _execution_result(rows=((2,),)),
    ) is False


def test_verify_execution_consistency_false_for_differing_columns():
    assert verify_execution_consistency(
        _execution_result(columns=("a",)), _execution_result(columns=("b",)),
    ) is False


def test_verify_execution_consistency_false_when_either_run_failed():
    assert verify_execution_consistency(
        _execution_result(status=EXEC_STATUS_ERROR), _execution_result(),
    ) is False
    assert verify_execution_consistency(
        _execution_result(), _execution_result(status=EXEC_STATUS_ERROR),
    ) is False


# ── No credentials/raw prompts ever carried ────────────────────────────────

def test_candidate_carries_no_secret_or_credential_fields():
    field_names = set(TrustedSqlExampleCandidate.__dataclass_fields__.keys())
    assert not any(
        "key" in name.lower() or "secret" in name.lower() or "token" in name.lower() or "password" in name.lower()
        for name in field_names
    )


# ── Step 9B: TrustedSqlExampleCandidate cannot be hand-constructed ────────

def test_direct_construction_without_guard_is_a_type_error():
    """Omitting the private construction-guard field entirely is a plain
    Python TypeError -- a required constructor argument is missing."""
    with pytest.raises(TypeError):
        TrustedSqlExampleCandidate(
            source_id=1, schema_fingerprint="fp-1", question_pattern="q", sql_template="SELECT 1",
            canonical_object_fqns=(), canonical_columns=(), join_path=(), result_shape=RESULT_SHAPE_COUNT,
            provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE, trust_level=TRUST_LEVEL_TRUSTED,
            search_document_text="x", search_document_hash="0" * 64, content_hash="0" * 64,
        )


def test_direct_construction_with_forged_guard_is_rejected():
    """Supplying some other object as the guard reaches __post_init__'s
    own explicit identity check, which raises ExampleCandidateRejected --
    only the exact sentinel build_trusted_example_candidate() itself holds
    is accepted."""
    with pytest.raises(ExampleCandidateRejected):
        TrustedSqlExampleCandidate(
            source_id=1, schema_fingerprint="fp-1", question_pattern="q", sql_template="SELECT 1",
            canonical_object_fqns=(), canonical_columns=(), join_path=(), result_shape=RESULT_SHAPE_COUNT,
            provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE, trust_level=TRUST_LEVEL_TRUSTED,
            search_document_text="x", search_document_hash="0" * 64, content_hash="0" * 64,
            _gate_guard=object(),
        )


def test_gate_produced_candidate_is_a_real_instance():
    """The one legitimate path -- build_trusted_example_candidate() itself
    -- produces a real, usable instance (sanity check that the guard
    mechanism doesn't also block the real gate)."""
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(), question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert isinstance(candidate, TrustedSqlExampleCandidate)
    assert candidate.trust_level == TRUST_LEVEL_TRUSTED


# ── Step 10: canonical_columns is DERIVED, never caller-supplied ──────────

def test_build_trusted_example_candidate_has_no_canonical_columns_parameter():
    """Structural guard: there is no way to pass a column list into the
    gate at all -- requirement 13, checked by signature, not just by
    behavior."""
    import inspect
    params = inspect.signature(build_trusted_example_candidate).parameters
    assert "canonical_columns" not in params


def test_canonical_columns_derived_from_validated_column_references():
    """Step 10A: encoded as 'object_fqn.column_name', never a bare
    column name -- see the object-ownership tests below for why."""
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="Status"),
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_columns == ("dbo.Student.Status", "dbo.Student.StudentID")


def test_canonical_columns_deduplicated_and_sorted():
    """Step 10A: the SAME bare column name on two DIFFERENT objects
    (dbo.Student.StudentID and dbo.Class.StudentID) must NOT collapse
    into one entry -- table ownership is preserved for both."""
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Class", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="FullName"),
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_columns == (
        "dbo.Class.StudentID", "dbo.Student.FullName", "dbo.Student.StudentID",
    )


def test_canonical_columns_empty_when_validation_result_carries_none():
    """A hand-built (or pre-Step-10-shaped) validation_result with no
    validated_column_references produces an empty canonical_columns --
    never an error, never a guess."""
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=_validation_result(validated_column_references=()),
        question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_columns == ()


def test_caller_cannot_inject_columns_via_any_other_parameter():
    """Requirement 13's full guarantee: build_trusted_example_candidate()
    accepts source_id/provenance/validation_result/question_pattern/
    result_shape/join_path/execution_result/repeat_execution_result and
    NOTHING else -- no parameter of any name could smuggle in a caller-
    chosen column list."""
    import inspect
    params = set(inspect.signature(build_trusted_example_candidate).parameters)
    assert params == {
        "source_id", "provenance", "validation_result", "question_pattern",
        "result_shape", "join_path", "execution_result", "repeat_execution_result",
    }


# ── Step 10A: trusted-example column identity ─────────────────────────────

def test_two_tables_both_referencing_id_keep_distinct_ownership():
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="id"),
            ValidatedColumnReference(object_fqn="dbo.Class", column_name="id"),
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert len(candidate.canonical_columns) == 2
    assert "dbo.Student.id" in candidate.canonical_columns
    assert "dbo.Class.id" in candidate.canonical_columns


def test_same_column_referenced_repeatedly_on_one_table_deduplicated():
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_columns == ("dbo.Student.StudentID",)


def test_case_insensitive_duplicates_collapse_to_one_canonical_entry():
    """Two references that differ only by casing on the SAME (object,
    column) identity collapse to one entry -- but see the next test for
    what must NEVER collapse (different objects)."""
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
            ValidatedColumnReference(object_fqn="dbo.Student", column_name="studentid"),
            ValidatedColumnReference(object_fqn="DBO.STUDENT", column_name="STUDENTID"),
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert len(candidate.canonical_columns) == 1
    assert candidate.canonical_columns[0].lower() == "dbo.student.studentid"


def test_same_named_columns_on_different_objects_never_collapse():
    """The exact Step 10A regression: dbo.A.Name and dbo.B.Name are
    DIFFERENT identities and must both survive, even though their bare
    column names are identical."""
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.A", column_name="Name"),
            ValidatedColumnReference(object_fqn="dbo.B", column_name="Name"),
            ValidatedColumnReference(object_fqn="dbo.A", column_name="Name"),  # exact duplicate -- collapses
        ),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert candidate.canonical_columns == ("dbo.A.Name", "dbo.B.Name")


def test_canonical_columns_ordering_is_deterministic_across_repeated_calls():
    validation_result = _validation_result(
        validated_column_references=(
            ValidatedColumnReference(object_fqn="dbo.Z", column_name="Last"),
            ValidatedColumnReference(object_fqn="dbo.A", column_name="First"),
            ValidatedColumnReference(object_fqn="dbo.A", column_name="Second"),
        ),
    )
    first = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    second = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert first.canonical_columns == second.canonical_columns == (
        "dbo.A.First", "dbo.A.Second", "dbo.Z.Last",
    )


def test_canonical_columns_output_is_an_immutable_tuple():
    validation_result = _validation_result(
        validated_column_references=(ValidatedColumnReference(object_fqn="dbo.A", column_name="X"),),
    )
    candidate = build_trusted_example_candidate(
        source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
        validation_result=validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
    )
    assert isinstance(candidate.canonical_columns, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.canonical_columns = ()


def test_stale_or_unsuccessful_validation_result_remains_ineligible():
    """A REJECTED (or otherwise non-OK) validation_result never reaches
    column derivation at all -- the existing gate 2 check (status == OK)
    still fires first, exactly as before Step 10A; canonical_columns is
    never populated from a non-OK result's validated_column_references,
    whatever they might (incorrectly) contain."""
    stale_result = _validation_result(
        status=VALIDATION_STATUS_REJECTED, error="schema drifted since this SQL was generated",
        validated_column_references=(ValidatedColumnReference(object_fqn="dbo.A", column_name="X"),),
    )
    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            validation_result=stale_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )


# ── Step 10A: wildcard boundary end-to-end (real validate_sql_candidate,
#    not a hand-built SqlValidationResult -- proves the actual pipeline
#    boundary, not just this module's own gate in isolation) ──────────────

def test_multi_table_wildcard_query_never_reaches_a_trusted_candidate():
    """A bare `SELECT *` over more than one visible source is rejected by
    core.sqlservice.sql_validation itself (column grounding cannot be
    reliably guaranteed) -- so build_trusted_example_candidate() can
    never even be handed an OK validation_result for one; it can never
    claim complete column grounding for a query whose wildcard expansion
    is unknown."""
    import hashlib
    from types import MappingProxyType

    from core.sqlservice.schema_context_retrieval import (
        SELECTION_REASON_SEMANTIC, STATUS_OK as SCHEMA_STATUS_OK, ContextDocument, SchemaContextResult,
    )
    from core.sqlservice.sql_generation_service import STATUS_OK as GEN_STATUS_OK, SqlGenerationCandidate, SqlGenerationResult
    from core.sqlservice.sql_validation import STATUS_REJECTED as VALIDATION_STATUS_REJECTED_REAL
    from core.sqlservice.sql_validation import validate_sql_candidate

    def _doc(fqn, columns):
        text = f"OBJECT: {fqn}"
        return ContextDocument(
            object_fqn=fqn, object_type="TABLE", retrieval_tier=1,
            document_hash=hashlib.sha256(text.encode()).hexdigest(), document_text=text,
            metadata=MappingProxyType({"column_names": tuple(columns)}), score=0.9,
            selection_reason=SELECTION_REASON_SEMANTIC,
        )

    docs = (_doc("dbo.A", ("X",)), _doc("dbo.B", ("Y",)))
    schema_context = SchemaContextResult(
        status=SCHEMA_STATUS_OK, build_id=1, schema_fingerprint="fp-1", embedding_provider="openai",
        embedding_model="m", embedding_dimensions=1, documents=docs, context_text="c", total_characters=1,
        truncated=False, warnings=(), error=None,
    )
    candidate_sql = SqlGenerationCandidate(
        sql="SELECT * FROM dbo.A a JOIN dbo.B b ON a.X = b.Y", interpretation="x",
        referenced_object_fqns=("dbo.A", "dbo.B"), assumptions=(),
        needs_clarification=False, clarification_question=None,
    )
    generation_result = SqlGenerationResult(
        status=GEN_STATUS_OK, candidate=candidate_sql, build_id=1, schema_fingerprint="fp-1",
        provider_id="p", model="m", warnings=(), error=None,
    )

    real_validation_result = validate_sql_candidate(generation_result, schema_context)
    assert real_validation_result.status == VALIDATION_STATUS_REJECTED_REAL
    assert "SELECT *" in real_validation_result.error

    with pytest.raises(ExampleCandidateRejected):
        build_trusted_example_candidate(
            source_id=1, provenance=PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
            validation_result=real_validation_result, question_pattern="q", result_shape=RESULT_SHAPE_COUNT,
        )
