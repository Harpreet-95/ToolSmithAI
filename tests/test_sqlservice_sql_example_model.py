"""
Focused unit tests — SQL rebuild Phase 5, Step 9: SqlExample data model,
trust policy, and SQL-template normalization
(core/sqlservice/sql_example_model.py).

Pure/offline: no database connection, no OpenAI call, no live SQL Server.

Run:
    venv/Scripts/pytest tests/test_sqlservice_sql_example_model.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.sqlservice.sql_example_model import (
    PROVENANCE_DEVELOPER_VERIFICATION_SET,
    PROVENANCE_OBSERVED_UNVERIFIED,
    PROVENANCE_SCHEMA_GENERATED_TEMPLATE,
    PROVENANCE_VALIDATED_EXECUTION,
    TRUST_LEVEL_TRUSTED,
    TRUST_LEVEL_UNTRUSTED,
    TemplateNormalizationError,
    _find_disallowed_literal_reasons,
    build_example_search_document,
    classify_trust_level,
    compute_content_hash,
    normalize_sql_template,
)


# ── Trust policy — no manual approval workflow ─────────────────────────────

def test_schema_generated_template_trusted_when_structurally_valid():
    assert classify_trust_level(
        PROVENANCE_SCHEMA_GENERATED_TEMPLATE, structural_validation_passed=True,
    ) == TRUST_LEVEL_TRUSTED


def test_schema_generated_template_untrusted_without_structural_validation():
    assert classify_trust_level(PROVENANCE_SCHEMA_GENERATED_TEMPLATE) == TRUST_LEVEL_UNTRUSTED


def test_developer_verification_set_always_trusted():
    assert classify_trust_level(PROVENANCE_DEVELOPER_VERIFICATION_SET) == TRUST_LEVEL_TRUSTED


def test_validated_execution_requires_all_four_gates():
    assert classify_trust_level(
        PROVENANCE_VALIDATED_EXECUTION,
        structural_validation_passed=True, read_only_validation_passed=True,
        execution_passed=True, consistency_passed=True,
    ) == TRUST_LEVEL_TRUSTED


def test_execution_success_alone_is_never_sufficient():
    """The exact requirement: 'Execution success alone must not make an
    example trusted.'"""
    assert classify_trust_level(PROVENANCE_VALIDATED_EXECUTION, execution_passed=True) == TRUST_LEVEL_UNTRUSTED


@pytest.mark.parametrize("missing_gate", ["structural_validation_passed", "read_only_validation_passed", "execution_passed", "consistency_passed"])
def test_validated_execution_untrusted_if_any_single_gate_missing(missing_gate):
    flags = {
        "structural_validation_passed": True, "read_only_validation_passed": True,
        "execution_passed": True, "consistency_passed": True,
    }
    flags[missing_gate] = False
    assert classify_trust_level(PROVENANCE_VALIDATED_EXECUTION, **flags) == TRUST_LEVEL_UNTRUSTED


def test_observed_unverified_never_trusted_even_with_every_flag_true():
    assert classify_trust_level(
        PROVENANCE_OBSERVED_UNVERIFIED,
        structural_validation_passed=True, read_only_validation_passed=True,
        execution_passed=True, consistency_passed=True,
    ) == TRUST_LEVEL_UNTRUSTED


def test_unrecognized_provenance_never_trusted():
    assert classify_trust_level("SOMETHING_ELSE", execution_passed=True) == TRUST_LEVEL_UNTRUSTED


# ── SQL template normalization ─────────────────────────────────────────────

def test_normalizes_string_and_numeric_literals():
    sql = "SELECT * FROM dbo.Student WHERE Status = 'Active' AND Age > 18"
    normalized = normalize_sql_template(sql)
    assert "'Active'" not in normalized
    assert ":string_literal" in normalized
    assert ":number_literal" in normalized
    assert "Age >" in normalized  # structural comparison retained


def test_retains_structural_joins_grouping_aggregation():
    sql = (
        "SELECT p.TrackName, COUNT(*) FROM dbo.Student s "
        "JOIN dbo.ClassSignups cs ON cs.StudentID = s.StudentID "
        "JOIN dbo.Class c ON c.ClassID = cs.ClassID "
        "JOIN dbo.Path p ON p.TrackID = c.TrackID "
        "GROUP BY p.TrackName"
    )
    normalized = normalize_sql_template(sql)
    # sqlglot's own canonical regeneration renders an implicit alias
    # explicitly (`dbo.ClassSignups cs` -> `dbo.ClassSignups AS cs`) --
    # structurally identical, still deterministic, never a substring edit
    # of the original text (see normalize_sql_template's own docstring).
    assert "JOIN dbo.ClassSignups AS cs ON cs.StudentID = s.StudentID" in normalized
    assert "GROUP BY p.TrackName" in normalized
    assert "COUNT(*)" in normalized


def test_deterministic_normalization():
    sql = "SELECT * FROM dbo.T WHERE X = 5"
    assert normalize_sql_template(sql) == normalize_sql_template(sql)


def test_rejects_blank_sql():
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("")
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("   ")


def test_rejects_non_read_only_statement():
    for bad in (
        "DELETE FROM dbo.Student",
        "UPDATE dbo.Student SET X = 1",
        "INSERT INTO dbo.Student VALUES (1)",
        "DROP TABLE dbo.Student",
        "EXEC sp_who",
    ):
        with pytest.raises(TemplateNormalizationError):
            normalize_sql_template(bad)


def test_rejects_multiple_statements():
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("SELECT 1; SELECT 2")


def test_rejects_unterminated_string_literal():
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("SELECT * FROM dbo.T WHERE X = 'unterminated")


def test_odbc_date_literal_normalized():
    sql = "SELECT * FROM dbo.T WHERE D >= {d '2026-01-01'}"
    normalized = normalize_sql_template(sql)
    assert "2026-01-01" not in normalized
    assert ":date_literal" in normalized


def test_odbc_timestamp_literal_normalized():
    sql = "SELECT * FROM dbo.T WHERE D >= {ts '2026-01-01 00:00:00'}"
    normalized = normalize_sql_template(sql)
    assert "2026-01-01" not in normalized
    assert ":date_literal" in normalized


# ── Adversarial tests — Phase 5, Step 9A parser-based normalization ───────
# sqlglot(dialect="tsql") real-AST authority, never regex-as-structure.

def test_escaped_single_quote_inside_string_literal_normalized():
    sql = "SELECT * FROM dbo.T WHERE Name = 'O''Brien'"
    normalized = normalize_sql_template(sql)
    assert "O'Brien" not in normalized
    assert ":string_literal" in normalized


def test_semicolon_inside_string_literal_is_not_a_second_statement():
    """A real parser (unlike naive `;` substring counting) correctly knows
    this is ONE statement — the semicolon is inside the string."""
    sql = "SELECT * FROM dbo.T WHERE Name = 'a;b'"
    normalized = normalize_sql_template(sql)
    assert ":string_literal" in normalized


def test_unicode_national_string_literal_normalized():
    sql = "SELECT * FROM dbo.T WHERE X = N'unicode éè value'"
    normalized = normalize_sql_template(sql)
    assert "unicode" not in normalized
    assert ":string_literal" in normalized


def test_decimal_literal_normalized():
    sql = "SELECT * FROM dbo.T WHERE Price = 19.99"
    normalized = normalize_sql_template(sql)
    assert "19.99" not in normalized
    assert ":number_literal" in normalized


def test_negative_number_literal_normalized_as_single_unit():
    sql = "SELECT * FROM dbo.T WHERE Balance = -5.5"
    normalized = normalize_sql_template(sql)
    assert "5.5" not in normalized
    assert ":number_literal" in normalized
    assert "-:number_literal" not in normalized  # the whole signed unit is replaced, never a bare leading '-'


def test_date_string_literal_in_comparison_normalized():
    sql = "SELECT * FROM dbo.T WHERE CreatedDate >= '2026-01-01'"
    normalized = normalize_sql_template(sql)
    assert "2026-01-01" not in normalized
    assert ":string_literal" in normalized


def test_in_list_literals_all_normalized():
    sql = "SELECT * FROM dbo.T WHERE Status IN ('Active', 'Pending', 'Closed')"
    normalized = normalize_sql_template(sql)
    assert "Active" not in normalized and "Pending" not in normalized and "Closed" not in normalized
    assert normalized.count(":string_literal") == 3


def test_between_bounds_normalized():
    sql = "SELECT * FROM dbo.T WHERE Age BETWEEN 18 AND 65"
    normalized = normalize_sql_template(sql)
    assert "18" not in normalized and "65" not in normalized
    assert normalized.count(":number_literal") == 2


def test_like_pattern_normalized():
    sql = "SELECT * FROM dbo.T WHERE Name LIKE '%smith%'"
    normalized = normalize_sql_template(sql)
    assert "smith" not in normalized
    assert ":string_literal" in normalized


def test_top_row_count_never_replaced():
    """TOP is a structural row-count control, never a filter value."""
    sql = "SELECT TOP 10 * FROM dbo.T WHERE X = 5"
    normalized = normalize_sql_template(sql)
    assert "TOP 10" in normalized or "TOP (10)" in normalized
    assert ":number_literal" in normalized  # the WHERE X = 5 filter value still gets replaced


def test_offset_fetch_never_replaced():
    sql = "SELECT * FROM dbo.T ORDER BY Name OFFSET 10 ROWS FETCH NEXT 20 ROWS ONLY"
    normalized = normalize_sql_template(sql)
    assert "10" in normalized
    assert "20" in normalized
    assert ":number_literal" not in normalized


def test_dateadd_date_part_identifier_stays_structural_but_delta_is_parameterized():
    """Step 9B: DATEADD's date-part identifier (`day`) is never a literal
    at all (sqlglot parses it as a plain Var) so it always stays
    structural -- but the numeric delta IS a user-variable value and must
    now be safely parameterized, never left as literal data."""
    sql = "SELECT * FROM dbo.T WHERE CreatedDate >= DATEADD(day, -30, GETDATE())"
    normalized = normalize_sql_template(sql)
    assert "DAY" in normalized.upper()  # the date-part identifier is untouched, still present
    assert "-30" not in normalized
    assert ":number_literal" in normalized


def test_nested_subquery_literals_normalized_in_their_own_context():
    sql = (
        "SELECT * FROM dbo.T t WHERE EXISTS ("
        "SELECT 1 FROM dbo.U u WHERE u.TID = t.ID AND u.Status = 'X')"
    )
    normalized = normalize_sql_template(sql)
    assert "'X'" not in normalized
    assert ":string_literal" in normalized
    # Step 9B: the EXISTS marker literal (a bare SELECT-list projection
    # constant) is no longer exempt either -- it gets parameterized too.
    assert "SELECT 1" not in normalized
    assert ":number_literal" in normalized


def test_case_expression_result_literals_now_parameterized():
    """Step 9B: CASE branch result constants are 'uncertain' literal
    positions (could be a real business label) -- no longer left as raw
    data, always parameterized."""
    sql = "SELECT CASE WHEN X = 1 THEN 'Y' ELSE 'N' END FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "'Y'" not in normalized and "'N'" not in normalized
    assert normalized.count(":string_literal") == 2  # both branch results
    assert ":number_literal" in normalized  # the X = 1 comparison still gets replaced


def test_coalesce_default_argument_now_parameterized():
    """Step 9B: COALESCE's default value argument is exactly the kind of
    'numeric function argument' that must be parameterized, not left as
    literal data."""
    sql = "SELECT COALESCE(X, 0) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "COALESCE(X, 0)" not in normalized
    assert "COALESCE(X, :number_literal)" in normalized


# ── Step 9B: literal-safety closure -- explicit required categories ───────

def test_select_projection_string_constant_parameterized():
    sql = "SELECT 'Active' AS Status, COUNT(*) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "'Active'" not in normalized
    assert ":string_literal" in normalized


def test_select_projection_number_constant_parameterized():
    sql = "SELECT 1 AS Flag, COUNT(*) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "1 AS Flag" not in normalized
    assert ":number_literal" in normalized


def test_positional_order_by_ordinal_never_replaced():
    sql = "SELECT Name, Age FROM dbo.T ORDER BY 1"
    normalized = normalize_sql_template(sql)
    assert "ORDER BY 1" in normalized
    assert ":number_literal" not in normalized


def test_cast_type_parameters_never_replaced():
    """DECIMAL(10,2)/VARCHAR(50)'s parameters are part of the SQL TYPE
    shape, not a data value -- structurally exempt like TOP/OFFSET/FETCH."""
    sql = "SELECT CAST(Price AS DECIMAL(10, 2)) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "(10, 2)" in normalized  # sqlglot canonicalizes DECIMAL -> NUMERIC; the type params themselves are untouched
    assert ":number_literal" not in normalized


def test_convert_type_parameter_never_replaced():
    sql = "SELECT CONVERT(VARCHAR(50), Name) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "VARCHAR(50)" in normalized
    assert ":number_literal" not in normalized


def test_binary_literal_parameterized():
    sql = "SELECT * FROM dbo.T WHERE Token = 0x1A2B"
    normalized = normalize_sql_template(sql)
    assert "1A2B" not in normalized
    assert ":binary_literal" in normalized


def test_nested_function_literal_inside_aggregate_parameterized():
    """A literal several levels deep inside a function call nested inside
    an aggregate -- still found and parameterized regardless of nesting
    depth (a real AST walk, not a shallow/one-level check)."""
    sql = "SELECT SUM(CASE WHEN Status = 'Active' THEN 100 ELSE 0 END) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "'Active'" not in normalized
    assert "100" not in normalized
    assert normalized.count(":number_literal") == 2  # the 100 and the 0
    assert ":string_literal" in normalized


def test_nested_function_literal_inside_left_length_argument():
    sql = "SELECT LEFT(Name, LEN(Name) - 3) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "3" not in normalized
    assert ":number_literal" in normalized


def test_literal_inside_dateadd_nested_inside_coalesce_all_parameterized():
    sql = "SELECT COALESCE(DATEADD(day, -7, CreatedDate), GETDATE()) FROM dbo.T"
    normalized = normalize_sql_template(sql)
    assert "-7" not in normalized
    assert "DAY" in normalized.upper()  # date-part identifier stays structural
    assert ":number_literal" in normalized


# ── Step 9B: final no-disallowed-literal invariant ─────────────────────────

def test_no_disallowed_literal_survives_a_real_normalization():
    """White-box: after a real normalize_sql_template() call, re-parsing
    the OUTPUT and running the same invariant scan finds nothing -- the
    invariant this module enforces internally holds for real output, not
    just in theory."""
    sql = (
        "SELECT TOP 5 CASE WHEN Status = 'Active' THEN 1 ELSE 0 END, "
        "COALESCE(Score, 0), CAST(Price AS DECIMAL(10,2)) "
        "FROM dbo.T WHERE Flag = 0x01 AND CreatedDate >= DATEADD(day, -30, GETDATE()) "
        "ORDER BY 1"
    )
    normalized = normalize_sql_template(sql)
    tree = sqlglot.parse_one(normalized, read="tsql")
    assert _find_disallowed_literal_reasons(tree) == ()


def test_invariant_scan_detects_a_literal_left_outside_the_allowlist():
    """White-box proof the invariant is a REAL, functioning safety net
    (not dead code): hand-build a tree with a literal sitting in a
    disallowed position (a bare arithmetic operand) that normal
    normalization would already have parameterized, and confirm the scan
    itself flags it rather than silently passing."""
    tree = sqlglot.parse_one("SELECT X + 5 FROM dbo.T", read="tsql")
    reasons = _find_disallowed_literal_reasons(tree)
    assert reasons != ()
    assert any("Literal" in r for r in reasons)


def test_invariant_scan_passes_a_hand_built_tree_with_only_exempt_literals():
    tree = sqlglot.parse_one("SELECT TOP 5 * FROM dbo.T ORDER BY 1", read="tsql")
    assert _find_disallowed_literal_reasons(tree) == ()


@pytest.mark.parametrize("sql", [
    "EXEC sp_executesql N'SELECT * FROM dbo.T'",
    "EXEC dbo.my_proc",
    "EXECUTE dbo.my_proc",
])
def test_dynamic_sql_and_exec_rejected(sql):
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM dbo.T -- trailing comment",
    "SELECT * FROM dbo.T /* block comment */",
    "SEL/**/ECT * FROM dbo.T",
])
def test_comment_variants_rejected(sql):
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template(sql)


@pytest.mark.parametrize("sql", [
    "INSERT INTO dbo.T (id) VALUES (1)",
    "UPDATE dbo.T SET X = 1",
    "DELETE FROM dbo.T",
    "DROP TABLE dbo.T",
    "SELECT * INTO #temp FROM dbo.T",
    "WITH x AS (DELETE FROM dbo.T OUTPUT deleted.*) SELECT * FROM x",
])
def test_writes_and_select_into_rejected(sql):
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM otherdb.dbo.T",
    "SELECT * FROM server1.otherdb.dbo.T",
    "SELECT * FROM #temp",
    "SELECT * FROM @tvar",
])
def test_cross_database_temp_and_table_variable_references_rejected(sql):
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template(sql)


def test_openrowset_family_rejected():
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("SELECT * FROM OPENQUERY(linkedserver, 'SELECT * FROM x')")


def test_nested_query_accepted_and_structurally_preserved():
    sql = "SELECT * FROM dbo.T WHERE ID IN (SELECT ID FROM dbo.U WHERE Status = 'Active')"
    normalized = normalize_sql_template(sql)
    assert "SELECT ID FROM dbo.U" in normalized
    assert ":string_literal" in normalized


def test_cte_literals_normalized_in_body():
    sql = "WITH ranked AS (SELECT id FROM dbo.T WHERE Status = 'X') SELECT * FROM ranked"
    normalized = normalize_sql_template(sql)
    assert "'X'" not in normalized
    assert ":string_literal" in normalized


def test_unparseable_garbage_rejected():
    with pytest.raises(TemplateNormalizationError):
        normalize_sql_template("SELEC BAD GARBAGE ###")


def test_normalization_is_deterministic_across_repeated_calls_for_adversarial_sql():
    sql = "SELECT TOP 5 * FROM dbo.T WHERE X IN (1,2,3) AND Y = 'z' ORDER BY 1"
    assert normalize_sql_template(sql) == normalize_sql_template(sql)


# ── Search-document construction ───────────────────────────────────────────

def test_search_document_includes_required_signals():
    doc = build_example_search_document(
        question_pattern="How many :entity are there",
        canonical_object_fqns=["dbo.Student", "dbo.ClassSignups"],
        join_path=["dbo.Student.id -> dbo.ClassSignups.student_id"],
        result_shape="COUNT",
    )
    assert "How many :entity are there" in doc
    assert "dbo.Student" in doc
    assert "dbo.ClassSignups" in doc
    assert "COUNT" in doc
    assert "->" in doc


def test_search_document_never_includes_sql_template():
    doc = build_example_search_document(
        question_pattern="q", canonical_object_fqns=["dbo.A"], join_path=[], result_shape="COUNT",
    )
    assert "SELECT" not in doc.upper() or "SELECT" not in doc  # sql_template text never passed in at all


def test_search_document_deterministic_regardless_of_object_order():
    a = build_example_search_document(
        question_pattern="q", canonical_object_fqns=["dbo.B", "dbo.A"], join_path=[], result_shape="COUNT",
    )
    b = build_example_search_document(
        question_pattern="q", canonical_object_fqns=["dbo.A", "dbo.B"], join_path=[], result_shape="COUNT",
    )
    assert a == b


# ── Content hash (immutable identity) ──────────────────────────────────────

def test_content_hash_deterministic():
    kwargs = dict(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.A"])
    assert compute_content_hash(**kwargs) == compute_content_hash(**kwargs)


def test_content_hash_independent_of_object_order():
    a = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.B", "dbo.A"])
    b = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.A", "dbo.B"])
    assert a == b


def test_content_hash_changes_with_source_id():
    a = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.A"])
    b = compute_content_hash(source_id=2, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.A"])
    assert a != b


def test_content_hash_changes_with_sql_template():
    a = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=["dbo.A"])
    b = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 2", canonical_object_fqns=["dbo.A"])
    assert a != b


def test_content_hash_is_hex64():
    h = compute_content_hash(source_id=1, question_pattern="q", sql_template="SELECT 1", canonical_object_fqns=[])
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ── No source-specific production identifiers ─────────────────────────────

_BANNED_DOMAIN_TOKENS = (
    "EDU_Student", "EDU_Class", "EDU_Track", "EDU_ClassSignups", "EDU_ClassEvent",
    "LMS_SUBSCRIPTION_PAYMENTS", "SurveyAnswers", "exampleco", "EXAMPLECO",
)


def test_no_domain_specific_tokens_in_production_module():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "core" / "sqlservice" / "sql_example_model.py").read_text(encoding="utf-8")
    for banned in _BANNED_DOMAIN_TOKENS:
        assert banned not in source, f"sql_example_model.py contains banned domain-specific token {banned!r}"
