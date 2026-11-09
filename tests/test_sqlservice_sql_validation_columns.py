"""
Focused unit tests — SQL rebuild Phase 5, Step 10: strict column-level SQL
validation, extending core/sqlservice/sql_validation.py's existing
table-level validator (core/sqlservice/sql_validation.py).

No database of any kind is used in this file — validate_sql_candidate()
takes an already-built SqlGenerationResult + SchemaContextResult directly,
so these tests construct both in memory, exactly like
tests/test_sqlservice_sql_validation.py already does. No OpenAI call, no
SQL execution, no live SQL Server or SQLite of any kind.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_validation_columns.py -v
"""

from __future__ import annotations

import dataclasses
import hashlib
from types import MappingProxyType

import pytest

from core.sqlservice.schema_context_retrieval import (
    SELECTION_REASON_SEMANTIC,
    STATUS_OK as SCHEMA_STATUS_OK,
    ContextDocument,
    SchemaContextResult,
)
from core.sqlservice.sql_generation_service import (
    STATUS_OK as GEN_STATUS_OK,
    SqlGenerationCandidate,
    SqlGenerationResult,
)
from core.sqlservice.sql_validation import (
    STATUS_OK,
    STATUS_REJECTED,
    SqlValidationResult,
    ValidatedColumnReference,
    validate_sql_candidate,
)


# ── Fixture-free helpers (mirrors test_sqlservice_sql_validation.py's own) ─


def _doc(fqn, *, tier=1, object_type="TABLE", columns=None, metadata_extra=None):
    text = f"OBJECT: {fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  (prose omitted)"
    metadata = dict(metadata_extra or {})
    if columns is not None:
        metadata["column_names"] = tuple(columns)
    return ContextDocument(
        object_fqn=fqn, object_type=object_type, retrieval_tier=tier,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType(metadata), score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _context(documents, *, build_id=1, status=SCHEMA_STATUS_OK, schema_fingerprint="fp-123", error=None):
    return SchemaContextResult(
        status=status, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536,
        documents=tuple(documents), context_text="context", total_characters=7,
        truncated=False, warnings=(), error=error,
    )


def _generation_result(sql, referenced, *, build_id=1, schema_fingerprint="fp-123"):
    candidate = SqlGenerationCandidate(
        sql=sql, interpretation="An interpretation.", referenced_object_fqns=tuple(referenced),
        assumptions=(), needs_clarification=False, clarification_question=None,
    )
    return SqlGenerationResult(
        status=GEN_STATUS_OK, candidate=candidate, build_id=build_id, schema_fingerprint=schema_fingerprint,
        provider_id="fake", model="fake-model", warnings=(), error=None,
    )


def _validate(sql, documents, *, referenced=None, **kwargs):
    docs = list(documents)
    gen = _generation_result(sql, referenced if referenced is not None else [d.object_fqn for d in docs])
    context = _context(docs)
    return validate_sql_candidate(gen, context, **kwargs)


def _confirmed_edge(column, other_fqn, other_column, *, direction="outbound", kind="PHYSICAL_FK"):
    """One core.sqlservice.retrieval_documents-shaped confirmed_relationship_edges
    entry — the real metadata shape core.sqlservice.sql_validation's Phase 7,
    Step 3A join validator reads (never document_text prose)."""
    return {"direction": direction, "column": column, "other": f"{other_fqn}.{other_column}", "kind": kind}


# ── 1. Single-table qualified and unqualified columns ──────────────────────

def test_valid_qualified_column():
    result = _validate(
        "SELECT s.StudentID FROM dbo.Student s",
        [_doc("dbo.Student", columns=("StudentID", "FullName"))],
    )
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (
        ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
    )


def test_valid_unqualified_column():
    result = _validate(
        "SELECT StudentID FROM dbo.Student",
        [_doc("dbo.Student", columns=("StudentID", "FullName"))],
    )
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (
        ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID"),
    )


# ── 2. Aliases and joins ────────────────────────────────────────────────────

def test_aliases_and_joins_columns_validated():
    sql = (
        "SELECT s.FullName, c.ClassName FROM dbo.Student s "
        "JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID "
        "JOIN dbo.Class c ON c.ClassID = cs.ClassID"
    )
    docs = [
        _doc("dbo.Student", columns=("StudentID", "FullName")),
        _doc(
            "dbo.ClassSignups", columns=("StudentID", "ClassID"),
            metadata_extra={"confirmed_relationship_edges": (
                _confirmed_edge("StudentID", "dbo.Student", "StudentID"),
                _confirmed_edge("ClassID", "dbo.Class", "ClassID"),
            )},
        ),
        _doc("dbo.Class", columns=("ClassID", "ClassName")),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "FullName"),
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.ClassSignups", "StudentID"),
        ValidatedColumnReference("dbo.ClassSignups", "ClassID"),
        ValidatedColumnReference("dbo.Class", "ClassID"),
        ValidatedColumnReference("dbo.Class", "ClassName"),
    }


# ── 3. Bracketed identifiers ────────────────────────────────────────────────

def test_bracketed_identifiers_resolved():
    result = _validate(
        "SELECT [s].[FullName] FROM dbo.[Student] AS [s] WHERE [s].[Status] = 'Active'",
        [_doc("dbo.Student", columns=("FullName", "Status"))],
    )
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "FullName"),
        ValidatedColumnReference("dbo.Student", "Status"),
    }


# ── 4. Unknown qualified column ─────────────────────────────────────────────

def test_unknown_qualified_column_rejected():
    result = _validate(
        "SELECT s.Ssn FROM dbo.Student s",
        [_doc("dbo.Student", columns=("StudentID", "FullName"))],
    )
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 5. Unknown unqualified column ───────────────────────────────────────────

def test_unknown_unqualified_column_rejected():
    result = _validate(
        "SELECT Ssn FROM dbo.Student",
        [_doc("dbo.Student", columns=("StudentID", "FullName"))],
    )
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 6. Ambiguous unqualified column ─────────────────────────────────────────

def test_ambiguous_unqualified_column_rejected():
    result = _validate(
        "SELECT StudentID FROM dbo.Student s JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID",
        [
            _doc("dbo.Student", columns=("StudentID", "FullName")),
            _doc("dbo.ClassSignups", columns=("StudentID", "ClassID")),
        ],
    )
    assert result.status == STATUS_REJECTED
    assert "ambiguous" in result.error


# ── 7. Invalid table alias ──────────────────────────────────────────────────

def test_invalid_table_alias_rejected():
    result = _validate(
        "SELECT z.FullName FROM dbo.Student s",
        [_doc("dbo.Student", columns=("StudentID", "FullName"))],
    )
    assert result.status == STATUS_REJECTED
    assert "not a valid table" in result.error


# ── 8. CTE output columns ───────────────────────────────────────────────────

def test_cte_output_columns_validated():
    sql = (
        "WITH ranked AS (SELECT StudentID, FullName FROM dbo.Student) "
        "SELECT r.FullName FROM ranked r"
    )
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.Student", "FullName"),
    }


def test_cte_output_column_not_projected_is_invalid():
    sql = (
        "WITH ranked AS (SELECT StudentID FROM dbo.Student) "
        "SELECT r.FullName FROM ranked r"
    )
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))], referenced=["dbo.Student"])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 9. Explicit CTE column list ─────────────────────────────────────────────

def test_explicit_cte_column_list_honored():
    sql = (
        "WITH x (A, B) AS (SELECT StudentID, FullName FROM dbo.Student) "
        "SELECT x.A, x.B FROM x"
    )
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error


def test_explicit_cte_column_list_rejects_original_inner_name():
    """Once a CTE declares an explicit column list, the OUTER query must
    use the new names, not the inner SELECT's own original aliases."""
    sql = (
        "WITH x (A, B) AS (SELECT StudentID, FullName FROM dbo.Student) "
        "SELECT x.StudentID FROM x"
    )
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))], referenced=["dbo.Student"])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 10. Nested subquery ─────────────────────────────────────────────────────

def test_nested_subquery_columns_validated():
    sql = (
        "SELECT t.StudentID, (SELECT COUNT(*) FROM dbo.ClassSignups cs WHERE cs.StudentID = t.StudentID) AS Cnt "
        "FROM dbo.Student t"
    )
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.ClassSignups", "StudentID"),
    }


def test_nested_subquery_unknown_column_rejected():
    sql = (
        "SELECT t.StudentID, (SELECT COUNT(*) FROM dbo.ClassSignups cs WHERE cs.Bogus = t.StudentID) AS Cnt "
        "FROM dbo.Student t"
    )
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 11. Correlated subquery ─────────────────────────────────────────────────

def test_correlated_subquery_outer_reference_validated():
    sql = (
        "SELECT * FROM dbo.Student t "
        "WHERE EXISTS (SELECT 1 FROM dbo.ClassSignups cs WHERE cs.StudentID = t.StudentID)"
    )
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references
    assert ValidatedColumnReference("dbo.ClassSignups", "StudentID") in result.validated_column_references


def test_correlated_subquery_unknown_outer_column_rejected():
    sql = (
        "SELECT * FROM dbo.Student t "
        "WHERE EXISTS (SELECT 1 FROM dbo.ClassSignups cs WHERE cs.StudentID = t.Bogus)"
    )
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 12. Derived-table aliases ───────────────────────────────────────────────

def test_derived_table_alias_columns_validated():
    sql = "SELECT d.X FROM (SELECT StudentID AS X FROM dbo.Student) AS d"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID",))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (ValidatedColumnReference("dbo.Student", "StudentID"),)


def test_derived_table_alias_explicit_columns_validated():
    sql = "SELECT d.P FROM (SELECT StudentID FROM dbo.Student) AS d(P)"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID",))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error


def test_derived_table_alias_unknown_column_rejected():
    sql = "SELECT d.Bogus FROM (SELECT StudentID AS X FROM dbo.Student) AS d"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID",))], referenced=["dbo.Student"])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 13. UNION / UNION ALL ───────────────────────────────────────────────────

def test_union_all_columns_validated_in_both_branches():
    sql = "SELECT StudentID FROM dbo.Student UNION ALL SELECT StudentID FROM dbo.Alumni"
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.Alumni", columns=("StudentID",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.Alumni", "StudentID"),
    }


def test_union_branch_unknown_column_rejected():
    sql = "SELECT StudentID FROM dbo.Student UNION ALL SELECT Bogus FROM dbo.Alumni"
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.Alumni", columns=("StudentID",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 14. SELECT alias in ORDER BY ────────────────────────────────────────────

def test_select_alias_in_order_by_accepted():
    result = _validate(
        "SELECT FullName AS N FROM dbo.Student ORDER BY N",
        [_doc("dbo.Student", columns=("FullName",))],
    )
    assert result.status == STATUS_OK, result.error


def test_select_alias_in_order_by_never_added_as_column_reference():
    result = _validate(
        "SELECT FullName AS N FROM dbo.Student ORDER BY N",
        [_doc("dbo.Student", columns=("FullName",))],
    )
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (ValidatedColumnReference("dbo.Student", "FullName"),)


def test_real_column_in_order_by_not_treated_as_alias_shadow():
    result = _validate(
        "SELECT FullName FROM dbo.Student ORDER BY StudentID",
        [_doc("dbo.Student", columns=("FullName", "StudentID"))],
    )
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references


# ── 15. Invalid alias use in WHERE/GROUP BY (real T-SQL forbids this) ─────

def test_select_alias_in_where_rejected():
    result = _validate(
        "SELECT FullName AS N FROM dbo.Student WHERE N = 'Alice'",
        [_doc("dbo.Student", columns=("FullName",))],
    )
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


def test_select_alias_in_group_by_rejected():
    result = _validate(
        "SELECT FullName AS N, COUNT(*) FROM dbo.Student GROUP BY N",
        [_doc("dbo.Student", columns=("FullName",))],
    )
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 16. Aggregate and window-function columns ───────────────────────────────

def test_aggregate_function_column_validated():
    result = _validate(
        "SELECT ClassID, SUM(Amount) FROM dbo.Payments GROUP BY ClassID",
        [_doc("dbo.Payments", columns=("ClassID", "Amount"))],
    )
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Payments", "ClassID"),
        ValidatedColumnReference("dbo.Payments", "Amount"),
    }


def test_window_function_columns_validated():
    sql = "SELECT SUM(x.Amount) OVER (PARTITION BY x.CategoryID ORDER BY x.PaidDate) FROM dbo.Payments x"
    result = _validate(sql, [_doc("dbo.Payments", columns=("Amount", "CategoryID", "PaidDate"))])
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Payments", "Amount"),
        ValidatedColumnReference("dbo.Payments", "CategoryID"),
        ValidatedColumnReference("dbo.Payments", "PaidDate"),
    }


def test_window_function_unknown_column_rejected():
    sql = "SELECT SUM(x.Amount) OVER (PARTITION BY x.Bogus) FROM dbo.Payments x"
    result = _validate(sql, [_doc("dbo.Payments", columns=("Amount", "CategoryID"))])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 17. CASE expressions ────────────────────────────────────────────────────

def test_case_expression_columns_validated():
    sql = "SELECT CASE WHEN s.Status = 'Active' THEN s.FullName ELSE 'N/A' END FROM dbo.Student s"
    result = _validate(sql, [_doc("dbo.Student", columns=("Status", "FullName"))])
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "Status"),
        ValidatedColumnReference("dbo.Student", "FullName"),
    }


def test_case_expression_unknown_column_rejected():
    sql = "SELECT CASE WHEN s.Bogus = 'Active' THEN 1 ELSE 0 END FROM dbo.Student s"
    result = _validate(sql, [_doc("dbo.Student", columns=("Status", "FullName"))])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── 18. COUNT(*) ─────────────────────────────────────────────────────────────

def test_count_star_permitted_regardless_of_visible_source_count():
    """COUNT(*) itself is never an exp.Column (sqlglot: Count(this=Star()))
    -- it contributes nothing to validated_column_references on its own;
    the JOIN's own ON condition still legitimately does."""
    sql = "SELECT COUNT(*) FROM dbo.Student s JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID"
    docs = [
        _doc("dbo.Student", columns=("StudentID",)),
        _doc(
            "dbo.ClassSignups", columns=("StudentID", "ClassID"),
            metadata_extra={"confirmed_relationship_edges": (_confirmed_edge("StudentID", "dbo.Student", "StudentID"),)},
        ),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (
        ValidatedColumnReference("dbo.ClassSignups", "StudentID"),
        ValidatedColumnReference("dbo.Student", "StudentID"),
    )


def test_count_star_alone_produces_no_column_references():
    result = _validate("SELECT COUNT(*) FROM dbo.Student", [_doc("dbo.Student", columns=("StudentID",))])
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == ()


# ── 19. Valid and invalid alias.* ───────────────────────────────────────────

def test_valid_alias_star_expands_to_all_columns():
    sql = "SELECT s.* FROM dbo.Student s JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID"
    docs = [
        _doc("dbo.Student", columns=("StudentID", "FullName")),
        _doc(
            "dbo.ClassSignups", columns=("StudentID", "ClassID"),
            metadata_extra={"confirmed_relationship_edges": (_confirmed_edge("StudentID", "dbo.Student", "StudentID"),)},
        ),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.Student", "FullName"),
        ValidatedColumnReference("dbo.ClassSignups", "StudentID"),
    }


def test_invalid_alias_star_rejected():
    sql = "SELECT z.* FROM dbo.Student s"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID",))])
    assert result.status == STATUS_REJECTED
    assert "does not reference a visible" in result.error


# ── 20. Bare SELECT * policy ─────────────────────────────────────────────────

def test_bare_select_star_allowed_for_single_visible_source():
    result = _validate("SELECT * FROM dbo.Student", [_doc("dbo.Student", columns=("StudentID", "FullName"))])
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.Student", "FullName"),
    }


def test_bare_select_star_rejected_for_multiple_visible_sources():
    sql = "SELECT * FROM dbo.Student s JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID"
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "bare SELECT *" in result.error


def test_bare_select_star_over_single_cte_allowed():
    sql = "WITH ranked AS (SELECT StudentID FROM dbo.Student) SELECT * FROM ranked"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID",))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error


def test_derived_table_alias_star_columns_still_fully_grounded_via_inner_scope():
    """Step 10A wildcard audit: d.* over a derived table adds nothing
    ITSELF to validated_column_references (see _expand_wildcard's own
    'derived' branch), but the REAL physical columns underneath it were
    already captured when the inner scope was validated -- so column
    grounding for a derived-table alias.* is still complete, never
    'unknown'."""
    sql = "SELECT d.* FROM (SELECT StudentID, FullName FROM dbo.Student) AS d"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))], referenced=["dbo.Student"])
    assert result.status == STATUS_OK, result.error
    assert set(result.validated_column_references) == {
        ValidatedColumnReference("dbo.Student", "StudentID"),
        ValidatedColumnReference("dbo.Student", "FullName"),
    }


# ── 21. Missing column_names metadata fails closed ──────────────────────────

def test_missing_column_names_metadata_fails_closed():
    result = _validate("SELECT StudentID FROM dbo.Student", [_doc("dbo.Student", columns=None)])
    assert result.status == STATUS_REJECTED
    assert "trustworthy structured column metadata" in result.error


def test_empty_column_names_metadata_fails_closed():
    result = _validate("SELECT StudentID FROM dbo.Student", [_doc("dbo.Student", columns=())])
    assert result.status == STATUS_REJECTED
    assert "trustworthy structured column metadata" in result.error


def test_non_string_column_names_entry_fails_closed():
    result = _validate(
        "SELECT StudentID FROM dbo.Student",
        [_doc("dbo.Student", metadata_extra={"column_names": ("StudentID", 123)})],
    )
    assert result.status == STATUS_REJECTED
    assert "trustworthy structured column metadata" in result.error


def test_wrong_type_column_names_fails_closed():
    result = _validate(
        "SELECT StudentID FROM dbo.Student",
        [_doc("dbo.Student", metadata_extra={"column_names": "StudentID"})],  # a bare string, not a list/tuple
    )
    assert result.status == STATUS_REJECTED
    assert "trustworthy structured column metadata" in result.error


def test_missing_metadata_fails_closed_even_when_table_not_column_referenced():
    """Requirement 4, read literally: a REFERENCED physical table lacking
    trustworthy metadata fails the whole candidate even if the query
    happens not to touch any of its columns directly (e.g. an unused
    JOIN target) -- untrustworthy metadata on a used table is never
    'safe enough because nothing asked about its columns'."""
    sql = "SELECT s.StudentID FROM dbo.Student s JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID"
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.ClassSignups", columns=None)]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "trustworthy structured column metadata" in result.error


# ── 22. Case-insensitive matching with canonical output spelling ──────────

def test_case_insensitive_column_match_preserves_canonical_spelling():
    result = _validate(
        "SELECT studentid FROM DBO.STUDENT",
        [_doc("dbo.Student", columns=("StudentID",))],
        referenced=["dbo.Student"],
    )
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (ValidatedColumnReference("dbo.Student", "StudentID"),)


def test_case_insensitive_qualified_column_match():
    result = _validate(
        "SELECT S.STUDENTID FROM dbo.Student AS S",
        [_doc("dbo.Student", columns=("StudentID",))],
    )
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (ValidatedColumnReference("dbo.Student", "StudentID"),)


# ── 23. Deterministic output ordering ───────────────────────────────────────

def test_validated_column_references_deterministically_ordered():
    sql = "SELECT s.FullName, s.StudentID, s.Status FROM dbo.Student s"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName", "Status"))])
    assert result.status == STATUS_OK, result.error
    names = [ref.column_name for ref in result.validated_column_references]
    assert names == sorted(names)


def test_validated_column_references_stable_across_repeated_calls():
    sql = "SELECT s.FullName, s.StudentID FROM dbo.Student s"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName"))]
    first = _validate(sql, docs)
    second = _validate(sql, docs)
    assert first.validated_column_references == second.validated_column_references


# ── 24. Duplicate references deduplicated deterministically ────────────────

def test_duplicate_column_references_deduplicated():
    sql = "SELECT s.StudentID, s.StudentID, s.FullName FROM dbo.Student s WHERE s.StudentID > 0"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName"))])
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references == (
        ValidatedColumnReference("dbo.Student", "FullName"),
        ValidatedColumnReference("dbo.Student", "StudentID"),
    )


def test_self_join_shared_column_deduplicated_to_one_reference():
    sql = "SELECT a.FullName, b.FullName FROM dbo.Student a JOIN dbo.Student b ON a.MentorID = b.StudentID"
    result = _validate(sql, [_doc("dbo.Student", columns=("StudentID", "FullName", "MentorID"))])
    assert result.status == STATUS_OK, result.error
    assert result.validated_column_references.count(ValidatedColumnReference("dbo.Student", "FullName")) == 1


# ── 25. Maximum-column-reference limit ──────────────────────────────────────

def test_max_column_references_limit_enforced():
    columns = tuple(f"Col{i}" for i in range(10))
    result = _validate(
        "SELECT * FROM dbo.Student", [_doc("dbo.Student", columns=columns)], max_column_references=5,
    )
    assert result.status == STATUS_REJECTED
    assert "exceeds the maximum" in result.error


def test_max_column_references_limit_not_triggered_when_under_limit():
    columns = tuple(f"Col{i}" for i in range(5))
    result = _validate(
        "SELECT * FROM dbo.Student", [_doc("dbo.Student", columns=columns)], max_column_references=10,
    )
    assert result.status == STATUS_OK, result.error


def test_max_column_references_invalid_configuration_raises():
    docs = [_doc("dbo.Student", columns=("StudentID",))]
    gen = _generation_result("SELECT StudentID FROM dbo.Student", ["dbo.Student"])
    with pytest.raises(ValueError):
        validate_sql_candidate(gen, _context(docs), max_column_references=0)


# ── 26. Immutable result objects ────────────────────────────────────────────

def test_validated_column_reference_is_frozen():
    ref = ValidatedColumnReference(object_fqn="dbo.Student", column_name="StudentID")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.column_name = "Other"


def test_sql_validation_result_column_references_field_is_a_tuple():
    result = _validate("SELECT StudentID FROM dbo.Student", [_doc("dbo.Student", columns=("StudentID",))])
    assert result.status == STATUS_OK, result.error
    assert isinstance(result.validated_column_references, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.validated_column_references = ()


# ── The previously observed 'EDU_ClassEvent.ClassName'-style failure ──────
#
# Mirrors a previously observed real failure mode (structurally, using
# generic identifiers rather than the original production table/column
# names, consistent with this codebase's convention of keeping domain-
# specific business identifiers out of generic test/production modules):
# an LLM-generated candidate referenced a plausible-looking
# "<EventTable>.<NameColumn>" qualified column that did not actually exist
# on that specific table (it existed, similarly named, on a DIFFERENT
# table) -- exactly the class of bug Step 10 exists to catch before the
# query ever reaches SQL Server or becomes a trusted few-shot example.

def test_previously_observed_plausible_but_nonexistent_column_rejected():
    sql = "SELECT e.EventName FROM dbo.ClassEvent e"
    docs = [
        _doc("dbo.ClassEvent", columns=("EventID", "ClassID", "EventDate")),  # no EventName/ClassName column
        _doc("dbo.Class", columns=("ClassID", "ClassName")),  # the similarly-named column lives HERE instead
    ]
    result = _validate(sql, docs, referenced=["dbo.ClassEvent"])
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


# ── Step 10A: correlated-reference resolution algorithm ───────────────────
#
# Local scope first; local ambiguity never falls back outward; only a
# ZERO-local-match column walks ancestor scopes nearest-to-farthest,
# stopping at (and only at) the first ancestor that supplies any match,
# rejecting immediately if THAT ancestor's own match is itself ambiguous.

def test_valid_unqualified_correlated_outer_reference():
    """The Step 10 regression this step fixes: a bare (unqualified)
    column that exists ONLY on an outer table, referenced from inside a
    correlated subquery, must resolve -- not be rejected as unknown."""
    sql = "SELECT * FROM dbo.Student t WHERE EXISTS (SELECT 1 FROM dbo.Payments p WHERE StudentID = t.StudentID)"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.Payments", columns=("PaymentID", "Amount"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references


def test_valid_qualified_correlated_outer_reference():
    sql = "SELECT * FROM dbo.Student t WHERE EXISTS (SELECT 1 FROM dbo.Payments p WHERE p.StudentID = t.StudentID)"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.Payments", columns=("StudentID", "Amount"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references
    assert ValidatedColumnReference("dbo.Payments", "StudentID") in result.validated_column_references


def test_local_column_shadows_outer_column_of_same_name():
    """Both the outer AND inner table have a 'StudentID' column -- the
    unqualified reference inside the subquery must resolve to the INNER
    (local) table, never the outer one, even though the outer one also
    has a column of that name."""
    sql = "SELECT t.FullName FROM dbo.Student t WHERE EXISTS (SELECT 1 FROM dbo.ClassSignups cs WHERE StudentID = 1)"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.ClassSignups", columns=("StudentID", "ClassID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.ClassSignups", "StudentID") in result.validated_column_references
    assert ValidatedColumnReference("dbo.Student", "StudentID") not in result.validated_column_references


def test_ambiguous_local_column_does_not_fall_back_outward():
    """Two LOCAL sources inside the subquery both expose 'Amount', and
    the OUTER table also has an 'Amount' column. This must be rejected as
    locally ambiguous -- never silently resolved against the outer
    table's own unambiguous 'Amount'."""
    sql = (
        "SELECT * FROM dbo.Ledger t WHERE EXISTS ("
        "SELECT 1 FROM dbo.Orders o JOIN dbo.Items i ON o.OrderID = i.ItemID WHERE Amount > 0)"
    )
    docs = [
        _doc("dbo.Ledger", columns=("Amount",)),
        _doc("dbo.Orders", columns=("OrderID", "Amount")),
        _doc("dbo.Items", columns=("ItemID", "Amount")),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "ambiguous" in result.error


def test_nearest_matching_ancestor_wins_two_level_nesting():
    sql = (
        "SELECT * FROM dbo.A a WHERE EXISTS ("
        "SELECT 1 FROM dbo.B b WHERE EXISTS ("
        "SELECT 1 FROM dbo.C c WHERE X = 1))"
    )
    docs = [_doc("dbo.A", columns=("X",)), _doc("dbo.B", columns=("Y",)), _doc("dbo.C", columns=("Z",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.A", "X") in result.validated_column_references


def test_nearer_ancestor_match_wins_over_farther_ancestor_with_same_column():
    """Two DIFFERENT ancestor levels both have a column of this name --
    the NEARER one must win, never the farther one, and never both."""
    sql = (
        "SELECT r.RootID FROM dbo.Root r WHERE EXISTS ("
        "SELECT 1 FROM dbo.Mid m WHERE EXISTS ("
        "SELECT 1 FROM dbo.Leaf lf WHERE Shared = 1))"
    )
    docs = [
        _doc("dbo.Root", columns=("RootID", "Shared")),
        _doc("dbo.Mid", columns=("Shared",)),
        _doc("dbo.Leaf", columns=("Z",)),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Mid", "Shared") in result.validated_column_references
    assert ValidatedColumnReference("dbo.Root", "Shared") not in result.validated_column_references
    assert ValidatedColumnReference("dbo.Root", "RootID") in result.validated_column_references


def test_ambiguous_nearest_ancestor_is_rejected():
    """The nearest ancestor that supplies ANY match has 2 visible sources
    both exposing the column -- rejected immediately as ambiguous at that
    level, never skipped in favor of a farther, unambiguous ancestor."""
    sql = (
        "SELECT * FROM dbo.P p, dbo.P2 p2 WHERE EXISTS ("
        "SELECT 1 FROM dbo.M m WHERE EXISTS ("
        "SELECT 1 FROM dbo.InnerTbl i WHERE Q = 1))"
    )
    docs = [
        _doc("dbo.P", columns=("Q",)), _doc("dbo.P2", columns=("Q",)),
        _doc("dbo.M", columns=("N",)), _doc("dbo.InnerTbl", columns=("Z",)),
    ]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "ambiguous" in result.error


def test_unknown_column_across_all_scopes_rejected():
    sql = "SELECT * FROM dbo.A a WHERE EXISTS (SELECT 1 FROM dbo.B b WHERE TotallyUnknownCol = 1)"
    docs = [_doc("dbo.A", columns=("X",)), _doc("dbo.B", columns=("Y",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "does not exist" in result.error


def test_two_level_nested_correlated_subquery_qualified():
    sql = (
        "SELECT * FROM dbo.A a WHERE EXISTS ("
        "SELECT 1 FROM dbo.B b WHERE EXISTS ("
        "SELECT 1 FROM dbo.C c WHERE c.Z = a.X AND c.Z = b.Y))"
    )
    docs = [_doc("dbo.A", columns=("X",)), _doc("dbo.B", columns=("Y",)), _doc("dbo.C", columns=("Z",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.A", "X") in result.validated_column_references
    assert ValidatedColumnReference("dbo.B", "Y") in result.validated_column_references
    assert ValidatedColumnReference("dbo.C", "Z") in result.validated_column_references


def test_correlated_reference_inside_exists():
    sql = "SELECT t.FullName FROM dbo.Student t WHERE EXISTS (SELECT 1 FROM dbo.Payments p WHERE p.StudentID = t.StudentID)"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.Payments", columns=("StudentID",))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references


def test_correlated_reference_inside_scalar_subquery():
    sql = "SELECT t.StudentID, (SELECT COUNT(*) FROM dbo.Payments p WHERE StudentID = t.StudentID) AS Cnt FROM dbo.Student t"
    docs = [_doc("dbo.Student", columns=("StudentID",)), _doc("dbo.Payments", columns=("PaymentID", "StudentID"))]
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error
    assert ValidatedColumnReference("dbo.Payments", "StudentID") in result.validated_column_references
    assert ValidatedColumnReference("dbo.Student", "StudentID") in result.validated_column_references


def test_deterministic_canonical_references_for_correlated_columns():
    sql = "SELECT * FROM dbo.Student t WHERE EXISTS (SELECT 1 FROM dbo.Payments p WHERE StudentID = t.StudentID)"
    docs = [_doc("dbo.Student", columns=("StudentID", "FullName")), _doc("dbo.Payments", columns=("PaymentID", "Amount"))]
    first = _validate(sql, docs)
    second = _validate(sql, docs)
    assert first.status == STATUS_OK, first.error
    assert first.validated_column_references == second.validated_column_references
    names = [r.column_name for r in first.validated_column_references]
    assert names == sorted(names)
