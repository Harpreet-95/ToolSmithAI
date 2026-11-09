"""
Focused tests — SQL rebuild Phase 5, Step 11A1, Problem 2: safe,
deterministic T-SQL identifier rendering
(core.sqlservice.sql_validation.render_tsql_identifier).

Pure/offline: no database of any kind, no OpenAI call, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_validation_identifiers.py -v
"""

from __future__ import annotations

import sqlglot
import pytest
from sqlglot import exp

from core.sqlservice.sql_validation import TSQL_DIALECT, render_tsql_identifier


def _round_trip_table_name(rendered: str) -> str:
    """Embed `rendered` as a table reference and recover the parsed
    (unrendered) identifier value sqlglot itself reads back out — the
    exact mechanism validate_sql_candidate uses to resolve a table
    reference against the schema-context object index."""
    sql = f"SELECT 1 FROM dbo.{rendered} AS t1"
    parsed = sqlglot.parse_one(sql, read=TSQL_DIALECT)
    table = next(parsed.find_all(exp.Table))
    return table.name


def _round_trip_column_name(rendered: str) -> str:
    sql = f"SELECT {rendered} FROM dbo.Widget AS t1"
    parsed = sqlglot.parse_one(sql, read=TSQL_DIALECT)
    column = next(parsed.find_all(exp.Column))
    return column.name


# ── Ordinary identifiers stay bare (no behavior change to existing
#    generated SQL for the common case) ────────────────────────────────────


@pytest.mark.parametrize("name", ["Widget", "widget_id", "WidgetID", "EDU_Student", "t1", "RowCount"])
def test_ordinary_identifier_rendered_bare(name):
    assert render_tsql_identifier(name) == name


# ── Bracket-rendered / literal-bracket identifiers ─────────────────────────


def test_identifier_containing_a_literal_bracket_pair_is_quoted_and_escaped():
    name = "[EDU_Project_Questions_28Jan2022]"
    rendered = render_tsql_identifier(name)
    assert rendered != name  # never embedded verbatim -- see round-trip proof below
    assert rendered.startswith("[") and rendered.endswith("]")
    assert _round_trip_table_name(rendered) == name


def test_identifier_with_a_trailing_literal_closing_bracket_is_escaped():
    name = "Foo]"
    rendered = render_tsql_identifier(name)
    assert _round_trip_table_name(rendered) == name


def test_identifier_with_a_literal_open_bracket_only():
    name = "Foo[Bar"
    rendered = render_tsql_identifier(name)
    assert _round_trip_table_name(rendered) == name


# ── Spaces ──────────────────────────────────────────────────────────────────


def test_identifier_with_a_space_is_quoted():
    name = "my column"
    rendered = render_tsql_identifier(name)
    assert rendered == "[my column]"
    assert _round_trip_column_name(rendered) == name


# ── Reserved words ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["TABLE", "DATE", "INDEX", "VALUES", "table", "Date"])
def test_reserved_keyword_identifier_is_quoted(name):
    rendered = render_tsql_identifier(name)
    assert rendered == f"[{name}]"
    assert _round_trip_table_name(rendered) == name


def test_non_reserved_word_that_merely_looks_keyword_ish_stays_bare():
    """'Order'/'Group' alone (not 'ORDER BY'/'GROUP BY') are not, in
    isolation, reserved single-word tokens in sqlglot's own TSQL tokenizer
    -- this documents that boundary precisely rather than assuming a
    broader keyword list than what sqlglot itself recognizes."""
    assert render_tsql_identifier("Order") == "Order"
    assert render_tsql_identifier("Group") == "Group"


# ── Punctuation ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["Foo-Bar", "Foo#Bar", "Foo@Bar", "Foo/Bar"])
def test_identifier_with_punctuation_is_quoted(name):
    rendered = render_tsql_identifier(name)
    assert rendered == f"[{name}]"
    assert _round_trip_table_name(rendered) == name


# ── Case-insensitive lookup, canonical spelling preserved ──────────────────


def test_rendering_preserves_exact_original_casing():
    for name in ("WidgetID", "widgetid", "WIDGETID", "[MixedCase_28Jan]"):
        rendered = render_tsql_identifier(name)
        assert _round_trip_table_name(rendered) == name  # exact casing recovered, never re-cased


# ── No duplicate identity caused by quoted vs. unquoted forms ──────────────


@pytest.mark.parametrize("name", [
    "Widget", "[EDU_Project_Questions_28Jan2022]", "my column", "TABLE", "Foo]", "123abc",
])
def test_no_split_brain_identity_between_raw_name_and_rendered_sql(name):
    """The whole point of render_tsql_identifier: embedding it in SQL text
    and later re-parsing that text always recovers the SAME identity a
    caller would get by using `name` directly as a dict key -- never a
    second, SQL-rendered identity that could silently diverge from the
    canonical one (the exact Step 11C defect)."""
    rendered = render_tsql_identifier(name)
    recovered = _round_trip_table_name(rendered)
    assert recovered == name
    assert recovered.lower() == name.lower()  # case-insensitive identity agreement too


# ── Generated SQL passes the real validator ────────────────────────────────


def test_rendered_identifier_sql_passes_real_validate_sql_candidate():
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
    from core.sqlservice.sql_validation import STATUS_OK as VALID_STATUS_OK, validate_sql_candidate

    object_fqn = "dbo.[EDU_Project_Questions_28Jan2022]"
    doc = ContextDocument(
        object_fqn=object_fqn, object_type="TABLE", retrieval_tier=1,
        document_hash="a" * 64, document_text="x",
        metadata=MappingProxyType({"column_names": ("ID",)}),
        score=0.9, selection_reason=SELECTION_REASON_SEMANTIC,
    )
    schema_context = SchemaContextResult(
        status=SCHEMA_STATUS_OK, build_id=1, schema_fingerprint="fp", embedding_provider="x",
        embedding_model="x", embedding_dimensions=1, documents=(doc,), context_text="x", total_characters=1,
        truncated=False, warnings=(), error=None,
    )
    rendered = render_tsql_identifier("dbo") + "." + render_tsql_identifier("[EDU_Project_Questions_28Jan2022]")
    sql = f"SELECT COUNT(*) AS RowCount FROM {rendered} AS t1"
    candidate = SqlGenerationCandidate(
        sql=sql, interpretation="x", referenced_object_fqns=(object_fqn,),
        assumptions=(), needs_clarification=False, clarification_question=None,
    )
    generation_result = SqlGenerationResult(
        status=GEN_STATUS_OK, candidate=candidate, build_id=1, schema_fingerprint="fp",
        provider_id="x", model="x", warnings=(), error=None,
    )
    result = validate_sql_candidate(generation_result, schema_context)
    assert result.status == VALID_STATUS_OK, result.error
    assert result.canonical_referenced_object_fqns == (object_fqn,)


# ── Contract violations ─────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", None, 123])
def test_invalid_name_raises(bad):
    with pytest.raises(ValueError):
        render_tsql_identifier(bad)
