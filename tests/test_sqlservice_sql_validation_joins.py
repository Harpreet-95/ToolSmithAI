"""
Focused tests — Phase 7, Step 3A, Part 3: deterministic confirmed-relationship
JOIN validation, extending core/sqlservice/sql_validation.py's existing
table/column-level validator with a real T-SQL AST-based join check.

Reproduces the verified Step 3 live-evaluation failure directly: AI
generation and AI review both approved `dbo.EDU_ClassSignups.ClassID =
dbo.vw_StudentCirriculum.ClassID` — two real, existing columns sharing a
name, with NO confirmed relationship behind them. This file's fixtures use
generic names, never the a real production database identifiers, but
test_same_named_columns_with_no_confirmed_relationship_rejected below is
that exact scenario's structural analogue.

No database of any kind is used in this file — validate_sql_candidate()
takes an already-built SqlGenerationResult + SchemaContextResult directly,
mirroring tests/test_sqlservice_sql_validation.py's and
tests/test_sqlservice_sql_validation_columns.py's own established
conventions exactly (including deliberately duplicating their small
fixture helpers rather than importing them — this codebase's own stated
per-module-helper convention). No OpenAI call, no SQL execution, no live
SQL Server or SQLite of any kind.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_validation_joins.py -v
"""

from __future__ import annotations

import hashlib
from types import MappingProxyType

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
    validate_sql_candidate,
)


# ── Fixture-free helpers ────────────────────────────────────────────────


def _doc(fqn, *, tier=1, object_type="TABLE", columns=("id",), metadata_extra=None, text=None):
    if text is None:
        text = f"OBJECT: {fqn}\nTYPE: {object_type}\nTIER: {tier}\nCOLUMNS:\n  (prose omitted)"
    metadata = {"column_names": tuple(columns)}
    metadata.update(metadata_extra or {})
    return ContextDocument(
        object_fqn=fqn, object_type=object_type, retrieval_tier=tier,
        document_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), document_text=text,
        metadata=MappingProxyType(metadata), score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )


def _confirmed_edge(column, other_fqn, other_column, *, direction="outbound", kind="PHYSICAL_FK"):
    return {"direction": direction, "column": column, "other": f"{other_fqn}.{other_column}", "kind": kind}


def _context(documents, *, build_id=1, schema_fingerprint="fp-123"):
    return SchemaContextResult(
        status=SCHEMA_STATUS_OK, build_id=build_id, schema_fingerprint=schema_fingerprint,
        embedding_provider="openai", embedding_model="text-embedding-3-small", embedding_dimensions=1536,
        documents=tuple(documents), context_text="context", total_characters=7,
        truncated=False, warnings=(), error=None,
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


# ── Shared fixture: A confirmed-FK-related to B via A.b_id = B.id ────────


def _a_b_docs(**b_metadata_extra):
    metadata_extra = {"confirmed_relationship_edges": (_confirmed_edge("b_id", "dbo.B", "id"),)}
    metadata_extra.update(b_metadata_extra)
    return [
        _doc("dbo.A", columns=("id", "b_id"), metadata_extra=metadata_extra),
        _doc("dbo.B", columns=("id", "name")),
    ]


# ── 1. Confirmed FK join accepted ─────────────────────────────────────────


def test_confirmed_fk_join_accepted():
    sql = "SELECT a.id, b.name FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


# ── 2. Reversed FK join accepted (direction and operand order) ───────────


def test_reversed_fk_join_accepted():
    sql = "SELECT a.id, b.name FROM dbo.A a JOIN dbo.B b ON b.id = a.b_id"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


def test_reversed_relationship_declaration_direction_accepted():
    """The SAME edge, declared from B's own side (inbound) instead of A's
    (outbound) -- confirmed_relationship_edges direction never matters,
    only the resulting undirected column-pair."""
    docs = [
        _doc("dbo.A", columns=("id", "b_id")),
        _doc(
            "dbo.B", columns=("id", "name"),
            metadata_extra={"confirmed_relationship_edges": (_confirmed_edge("id", "dbo.A", "b_id", direction="inbound"),)},
        ),
    ]
    sql = "SELECT a.id FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id"
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error


# ── 3. Confirmed view-safe executable relationship accepted ──────────────


def test_confirmed_view_safe_executable_relationship_accepted():
    """A CATALOG_VIEW_DEPENDENCY-kind edge -- sourced from view-dependency
    discovery rather than a declared FK -- is still a genuine, classified-
    confirmed, column-level relationship (build_retrieval_documents()
    itself requires both from_column and to_column to record ANY confirmed
    edge, regardless of kind) and is trusted exactly like a PHYSICAL_FK
    one."""
    docs = [
        _doc(
            "dbo.SummaryView", object_type="VIEW", columns=("id", "a_id"),
            metadata_extra={
                "confirmed_relationship_edges": (_confirmed_edge("a_id", "dbo.A", "id", kind="CATALOG_VIEW_DEPENDENCY"),),
            },
        ),
        _doc("dbo.A", columns=("id",)),
    ]
    sql = "SELECT v.id FROM dbo.SummaryView v JOIN dbo.A a ON v.a_id = a.id"
    result = _validate(sql, docs)
    assert result.status == STATUS_OK, result.error


# ── 4. Lineage-only view dependency rejected as a join ────────────────────


def test_lineage_only_view_dependency_rejected_as_join():
    """A view whose ONLY relationship signal is architectural/lineage --
    e.g. mentioned in document prose, or a bare object-level reference with
    no column-pair-confirmed edge at all -- must never authorize a join.
    This validator reads ONLY confirmed_relationship_edges; document_text
    prose (even if it says "RELATIONSHIPS_CONFIRMED: dbo.A") is never
    consulted."""
    docs = [
        _doc(
            "dbo.SummaryView", object_type="VIEW", columns=("id", "a_id"),
            text=(
                "OBJECT: dbo.SummaryView\nTYPE: VIEW\nTIER: 1\nCOLUMNS:\n  (prose omitted)\n"
                "RELATIONSHIPS_CONFIRMED:\n  a_id -> dbo.A.id"  # prose mention only, NOT structured metadata
            ),
        ),
        _doc("dbo.A", columns=("id",)),
    ]
    sql = "SELECT v.id FROM dbo.SummaryView v JOIN dbo.A a ON v.a_id = a.id"
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "confirmed relationship" in result.error


# ── 5. Same-named-column join with no confirmed relationship rejected ────
#     (the exact structural shape of the verified Step 3 failure)


def test_same_named_columns_with_no_confirmed_relationship_rejected():
    docs = [
        _doc("dbo.Signups", columns=("id", "class_id")),
        _doc("dbo.UnrelatedView", object_type="VIEW", columns=("id", "class_id")),  # same column name, no edge
    ]
    sql = "SELECT s.id FROM dbo.Signups s JOIN dbo.UnrelatedView v ON s.class_id = v.class_id"
    result = _validate(sql, docs)
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


# ── 6. Wrong columns on an otherwise-confirmed object pair rejected ──────


def test_wrong_columns_on_confirmed_object_pair_rejected():
    """dbo.A and dbo.B DO have a confirmed relationship (a.b_id = b.id) —
    but the SQL joins them on a COMPLETELY DIFFERENT, unconfirmed column
    pair. Object-pair-level confirmation is never sufficient; the exact
    column pair must match."""
    docs = _a_b_docs()
    sql = "SELECT a.id FROM dbo.A a JOIN dbo.B b ON a.id = b.name"  # a.id/b.name -- not the confirmed pair
    result = _validate(sql, docs, referenced=["dbo.A", "dbo.B"])
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


# ── 7. Alias join accepted ─────────────────────────────────────────────


def test_alias_join_accepted():
    sql = "SELECT x.id, y.name FROM dbo.A AS x JOIN dbo.B AS y ON x.b_id = y.id"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


# ── 8. CTE/subquery-hidden unsupported join rejected ──────────────────────


def test_cte_hidden_unsupported_join_rejected():
    docs = [
        _doc("dbo.A", columns=("id", "b_id")),
        _doc("dbo.C", columns=("id", "value")),  # no relationship to A at all
    ]
    sql = (
        "WITH ranked AS (SELECT a.id, c.value FROM dbo.A a JOIN dbo.C c ON a.b_id = c.id) "
        "SELECT * FROM ranked"
    )
    result = _validate(sql, docs, referenced=["dbo.A", "dbo.C"])
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


def test_subquery_hidden_unsupported_join_rejected():
    docs = [
        _doc("dbo.A", columns=("id", "b_id")),
        _doc("dbo.C", columns=("id", "value")),
    ]
    sql = "SELECT * FROM (SELECT a.id, c.value FROM dbo.A a JOIN dbo.C c ON a.b_id = c.id) sub"
    result = _validate(sql, docs, referenced=["dbo.A", "dbo.C"])
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


def test_cte_with_confirmed_join_accepted():
    sql = (
        "WITH ranked AS (SELECT a.id, b.name FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id) "
        "SELECT * FROM ranked"
    )
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


# ── 9. Cross/comma join rejected (no connecting equality anywhere) ───────


def test_comma_join_with_no_connecting_predicate_rejected():
    sql = "SELECT a.id, b.name FROM dbo.A a, dbo.B b"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


def test_cross_join_keyword_with_no_connecting_predicate_rejected():
    sql = "SELECT a.id, b.name FROM dbo.A a CROSS JOIN dbo.B b"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


def test_comma_join_with_confirmed_where_equality_accepted():
    """A comma-join is not inherently a bypass -- when a confirmed equality
    genuinely connects the two tables (even via WHERE, not ON), it is
    accepted, matching ordinary SQL-89 join style."""
    sql = "SELECT a.id, b.name FROM dbo.A a, dbo.B b WHERE a.b_id = b.id"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


# ── 10. `ON 1=1` rejected ─────────────────────────────────────────────────


def test_on_1_equals_1_rejected():
    sql = "SELECT a.id, b.name FROM dbo.A a JOIN dbo.B b ON 1 = 1"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


# ── 11. Disconnected multi-object query rejected ──────────────────────────


def test_disconnected_multi_object_query_rejected():
    """Three objects: A confirmed-joined to B, but C entirely disconnected
    from both -- must reject even though PART of the query is legitimate."""
    docs = _a_b_docs() + [_doc("dbo.C", columns=("id", "value"))]
    sql = "SELECT a.id, b.name, c.value FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id, dbo.C c"
    result = _validate(sql, docs, referenced=["dbo.A", "dbo.B", "dbo.C"])
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


# ── 12. Single-object query unchanged ─────────────────────────────────────


def test_single_object_query_still_accepted():
    result = _validate("SELECT a.id, a.b_id FROM dbo.A a", [_doc("dbo.A", columns=("id", "b_id"))])
    assert result.status == STATUS_OK, result.error


def test_self_join_never_requires_a_confirmed_relationship():
    """A self-join (same physical object, two aliases) is never subject to
    the cross-object confirmation requirement -- there is only ONE distinct
    physical object in scope."""
    sql = "SELECT a1.id FROM dbo.A a1 JOIN dbo.A a2 ON a1.b_id = a2.id"
    result = _validate(sql, [_doc("dbo.A", columns=("id", "b_id"))], referenced=["dbo.A"])
    assert result.status == STATUS_OK, result.error


# ── 13. UNION branches handled correctly ──────────────────────────────────


def test_union_branches_each_independently_validated():
    sql = (
        "SELECT a.id FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id "
        "UNION "
        "SELECT a.id FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id"
    )
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_OK, result.error


def test_union_branch_with_unsupported_join_rejected():
    """One valid branch, one branch with an unsupported join -- the whole
    candidate is rejected; a partially-safe query is never partially
    executed."""
    docs = _a_b_docs() + [_doc("dbo.C", columns=("id", "value"))]
    sql = (
        "SELECT a.id FROM dbo.A a JOIN dbo.B b ON a.b_id = b.id "
        "UNION "
        "SELECT a.id FROM dbo.A a JOIN dbo.C c ON a.id = c.id"
    )
    result = _validate(sql, docs, referenced=["dbo.A", "dbo.B", "dbo.C"])
    assert result.status == STATUS_REJECTED
    assert "no confirmed relationship" in result.error


# ── 14. Sanitized, typed error — never raw SQL/credential/driver text ────


def test_rejection_error_is_code_authored_and_bounded():
    sql = "SELECT a.id, b.name FROM dbo.A a JOIN dbo.B b ON 1 = 1"
    result = _validate(sql, _a_b_docs())
    assert result.status == STATUS_REJECTED
    assert isinstance(result.error, str)
    assert "PWD" not in result.error and "Server=" not in result.error
