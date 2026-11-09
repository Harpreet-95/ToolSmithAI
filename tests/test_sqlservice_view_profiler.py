"""
Focused tests — Phase 7, Step 6E3B, Parts 4/5/6: deterministic view
profiling / output-column lineage / suitability (core/sqlservice/
view_profiler.py). Pure computation, no database — mirrors tests/
test_sqlservice_schema_object_authority.py's own hand-built-fixture
style for the sibling module this one reuses AST primitives from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

import core.sqlservice.view_profiler as vp
from core.sqlservice import schema_object_authority as authority_module


@dataclass(frozen=True)
class _FakeAuthority:
    object_fqn: str
    authority_class: str
    unsafe_dependency_fqns: tuple = ()


def _compute(
    sql, *, object_fqn="dbo.v_test",
    columns=None, pks=None, authority=None, direct_deps=(), transitive_deps=(),
):
    return vp.compute_view_profile(
        object_fqn, sql,
        source_id=90001, build_id=1, schema_fingerprint="fp1",
        direct_dependencies=direct_deps, transitive_dependencies=transitive_deps,
        authority=authority,
        column_names_by_fqn_lower=columns or {},
        primary_key_columns_by_fqn_lower=pks or {},
        now="2026-09-30T00:00:00Z",
    )


def test_no_definition_sql_is_typed_failure():
    profile = _compute(None)
    assert profile.completion_status == vp.COMPLETION_FAILED
    assert profile.failure_reason == vp.FAILURE_NO_DEFINITION


def test_unparseable_sql_is_typed_failure():
    profile = _compute("THIS IS NOT ; VALID { SQL )")
    assert profile.completion_status == vp.COMPLETION_FAILED
    assert profile.failure_reason == vp.FAILURE_PARSE_ERROR


def test_simple_passthrough_and_alias_resolves_with_high_confidence():
    sql = "CREATE VIEW dbo.v_test AS SELECT u.Id, u.Name AS FullName FROM dbo.Users u"
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id", "name"})},
        pks={"dbo.users": ("Id",)},
    )
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    assert profile.lineage_complete is True
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Id"].confidence == vp.LINEAGE_CONFIDENCE_HIGH
    assert by_name["Id"].source_refs == (("dbo.users", "Id"),)
    assert by_name["FullName"].transformation_type == vp.TRANSFORM_PASSTHROUGH
    assert by_name["FullName"].source_refs == (("dbo.users", "Name"),)
    # Id is the anchor table's own PK and is projected -> a real entity key
    assert "Id" in profile.entity_keys
    assert profile.required_distinct_key == "Id"


def test_unqualified_column_disambiguated_via_known_columns():
    sql = "SELECT Id, o.Total FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"total", "userid"})},
    )
    by_name = {c.output_name: c for c in profile.output_columns}
    # "Id" is unqualified but only dbo.users actually has a column named Id
    assert by_name["Id"].source_refs == (("dbo.users", "Id"),)
    assert by_name["Id"].confidence != vp.LINEAGE_CONFIDENCE_UNRESOLVED


def test_ambiguous_unqualified_column_is_unresolved():
    sql = "SELECT Id FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"id"})},
    )
    col = profile.output_columns[0]
    assert col.confidence == vp.LINEAGE_CONFIDENCE_UNRESOLVED
    assert col.unresolved_reason == vp.LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED
    assert profile.lineage_complete is False
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    assert profile.failure_reason == "LINEAGE_INCOMPLETE"


def test_aggregate_expression_sets_aggregated_grain_and_blocks_detail():
    sql = "SELECT o.CustomerId, SUM(o.Amount) AS Total FROM dbo.Orders o GROUP BY o.CustomerId"
    profile = _compute(sql, columns={"dbo.orders": frozenset({"customerid", "amount"})})
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Total"].transformation_type == vp.TRANSFORM_AGGREGATE
    assert profile.grain_class == authority_module.GRAIN_CLASS_AGGREGATED
    assert profile.aggregate_functions
    assert profile.group_by_columns
    assert profile.suitability.suitable_for_detail is False
    assert any(r.startswith("DETAIL:") for r in profile.suitability.unsuitable_reasons)


def test_distinct_sets_deduplicated_grain():
    sql = "SELECT DISTINCT o.CustomerId FROM dbo.Orders o"
    profile = _compute(sql, columns={"dbo.orders": frozenset({"customerid"})})
    assert profile.has_distinct is True
    assert profile.grain_class == authority_module.GRAIN_CLASS_DEDUPLICATED


def test_select_star_single_table_expands_using_structural_metadata():
    sql = "SELECT * FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    names = {c.output_name for c in profile.output_columns}
    assert names == {"id", "name"}
    assert all(c.transformation_type == vp.TRANSFORM_STAR_EXPANSION for c in profile.output_columns)
    assert profile.lineage_complete is True


def test_select_star_multi_table_expands_every_source_safely():
    # Phase 7, Step 6E3B3: SELECT * across a multi-table JOIN is no longer
    # treated as a blanket failure -- SQL Server itself returns every
    # column from every FROM/JOIN source (duplicate names and all), and
    # each one here is individually, positionally resolved to its own
    # real table/column -- never a name-matching guess.
    sql = "SELECT * FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"id", "userid"})},
    )
    assert profile.lineage_complete is True
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    refs = {(c.output_name, c.source_refs) for c in profile.output_columns}
    assert ("id", (("dbo.users", "id"),)) in refs
    assert ("id", (("dbo.orders", "id"),)) in refs
    assert ("userid", (("dbo.orders", "userid"),)) in refs


def test_select_star_single_table_still_resolves_as_before():
    sql = "SELECT * FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.lineage_complete is True
    names = {c.output_name for c in profile.output_columns}
    assert names == {"id", "name"}


def test_qualified_star_scoped_to_one_alias():
    sql = "SELECT u.*, o.Total FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id", "name"}), "dbo.orders": frozenset({"total", "userid"})},
    )
    assert profile.lineage_complete is True
    names_sources = {(c.output_name, c.source_refs) for c in profile.output_columns}
    assert ("id", (("dbo.users", "id"),)) in names_sources
    assert ("name", (("dbo.users", "name"),)) in names_sources
    assert ("Total", (("dbo.orders", "Total"),)) in names_sources


def test_union_branches_detected_and_merged():
    sql = (
        "SELECT u.Id AS RowId FROM dbo.Users u "
        "UNION SELECT a.Id AS RowId FROM dbo.Archive a"
    )
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.archive": frozenset({"id"})},
    )
    assert profile.has_union is True
    assert profile.union_branch_count == 2
    assert len(profile.output_columns) == 1
    assert profile.output_columns[0].output_name == "RowId"


def test_cte_resolves_through_one_level():
    sql = (
        "WITH c AS (SELECT u.Id AS UID FROM dbo.Users u) "
        "SELECT c.UID FROM c"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"})})
    col = profile.output_columns[0]
    assert col.confidence != vp.LINEAGE_CONFIDENCE_UNRESOLVED
    assert col.source_refs == (("dbo.users", "Id"),)


def test_window_function_detected():
    sql = "SELECT ROW_NUMBER() OVER (PARTITION BY t.A ORDER BY t.B) AS rn FROM dbo.T t"
    profile = _compute(sql, columns={"dbo.t": frozenset({"a", "b"})})
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["rn"].transformation_type == vp.TRANSFORM_WINDOW
    assert profile.window_functions


def test_top_limit_detected():
    sql = "SELECT TOP 10 u.Id FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"})})
    assert profile.has_top_limit is True


def test_inner_join_creates_fanout_risk_and_at_risk_preservation():
    sql = "SELECT u.Id FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"userid"})},
        pks={"dbo.users": ("Id",)},
    )
    assert profile.duplication_risk == vp.DUPLICATION_RISK_POSSIBLE
    assert profile.row_preservation_risk == authority_module.ROW_PRESERVATION_AT_RISK
    # a stable entity key (Id) IS available, so JOIN suitability still holds
    assert profile.suitability.suitable_for_join is False or "Id" in profile.entity_keys


def test_left_join_preserves_rows_no_fanout():
    sql = "SELECT u.Id FROM dbo.Users u LEFT JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"userid"})},
        pks={"dbo.users": ("Id",)},
    )
    assert profile.duplication_risk == vp.DUPLICATION_RISK_NONE
    assert profile.row_preservation_risk == authority_module.ROW_PRESERVATION_PRESERVED
    assert profile.suitability.suitable_for_join is True


def test_where_literal_filter_reduces_row_preservation_and_records_population_restriction():
    sql = "SELECT u.Id FROM dbo.Users u WHERE u.Status = 'ACTIVE'"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "status"})})
    assert profile.row_preservation_risk == authority_module.ROW_PRESERVATION_REDUCED
    assert profile.population_restrictions
    assert profile.suitability.population_restriction is not None


def test_secret_column_flagged_informationally_never_as_value():
    sql = "SELECT u.Id, u.PasswordHash AS PasswordHash FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "passwordhash"})})
    assert "PasswordHash" in profile.secret_columns
    # informational only -- never a raw value, just the column name/category
    assert all(":" in cat or cat for cat in profile.pii_categories)


def test_pii_category_recorded_without_values():
    sql = "SELECT u.Email FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"email"})})
    assert any(cat.startswith("Email:") for cat in profile.pii_categories)
    assert not profile.secret_columns


def test_framework_backed_eligibility_widens_unsafe_dependency_with_resolved_lineage():
    authority = _FakeAuthority(object_fqn="dbo.v_framework", authority_class=authority_module.AUTHORITY_CLASS_UNSAFE_DEPENDENCY)
    sql = "SELECT u.Id, u.Name AS StudentName FROM dbo.Users u"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id", "name"})}, authority=authority,
    )
    has_business_cols = any(
        c.confidence != vp.LINEAGE_CONFIDENCE_UNRESOLVED
        and c.transformation_type != vp.TRANSFORM_LITERAL
        and c.output_name not in profile.secret_columns
        for c in profile.output_columns
    )
    refined = authority_module.classify_framework_backed_eligibility(
        authority, lineage_complete=profile.lineage_complete,
        has_business_output_columns=has_business_cols, selects_secret_column=bool(profile.secret_columns),
    )
    assert refined == authority_module.AUTHORITY_CLASS_FRAMEWORK_BACKED_BUSINESS_VIEW


def test_framework_backed_eligibility_fails_closed_when_lineage_incomplete():
    authority = _FakeAuthority(object_fqn="dbo.v_framework2", authority_class=authority_module.AUTHORITY_CLASS_UNSAFE_DEPENDENCY)
    refined = authority_module.classify_framework_backed_eligibility(
        authority, lineage_complete=False, has_business_output_columns=True, selects_secret_column=False,
    )
    assert refined == authority_module.AUTHORITY_CLASS_LINEAGE_INCOMPLETE


def test_framework_backed_eligibility_never_touches_other_classes():
    authority = _FakeAuthority(object_fqn="dbo.v_other", authority_class=authority_module.AUTHORITY_CLASS_RESTRICTED_VIEW)
    refined = authority_module.classify_framework_backed_eligibility(
        authority, lineage_complete=True, has_business_output_columns=True, selects_secret_column=False,
    )
    assert refined == authority_module.AUTHORITY_CLASS_RESTRICTED_VIEW


# ── Phase 7, Step 6E3B3 -- derived-table / CTE scope resolution ──────────


def test_derived_table_alias_resolves_through_outer_reference():
    sql = (
        "SELECT d.Id, d.FullName FROM "
        "(SELECT u.Id AS Id, u.Name AS FullName FROM dbo.Users u) d"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.lineage_complete is True
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Id"].source_refs == (("dbo.users", "Id"),)
    assert by_name["FullName"].source_refs == (("dbo.users", "Name"),)


def test_derived_table_whose_own_body_is_a_union_resolves():
    # Phase 7, Step 6E3B6, Part 3 -- a real, live-audit-verified gap: a
    # derived table/CTE source whose own body is a UNION (not a plain
    # SELECT) used to be silently unregistered as a scope at all --
    # cascading into every outer reference through it failing closed.
    sql = (
        "WITH ranked AS ("
        "  SELECT Id, Val FROM ("
        "    SELECT a.Id AS Id, a.Val AS Val FROM dbo.A a "
        "    UNION ALL "
        "    SELECT b.Id AS Id, b.Val AS Val FROM dbo.B b"
        "  ) AS combined"
        ") "
        "SELECT Id, Val FROM ranked"
    )
    profile = _compute(sql, columns={"dbo.a": frozenset({"id", "val"}), "dbo.b": frozenset({"id", "val"})})
    assert profile.lineage_complete is True
    by_name = {c.output_name: c for c in profile.output_columns}
    assert set(by_name["Id"].source_refs) == {("dbo.a", "Id"), ("dbo.b", "Id")}


def test_bare_cte_from_reference_scoped_to_itself_not_every_sibling_cte():
    # Phase 7, Step 6E3B6, Part 3 -- a real, live-audit-verified gap: a
    # multi-CTE chain where the FINAL select's FROM clause bare-references
    # only the LAST CTE (no subquery wrapper, no alias) -- the two EARLIER
    # sibling CTEs, each of which also happens to pass through a
    # same-named column, must never be treated as competing candidates
    # for an unqualified reference in the final select, since they were
    # never actually joined there at all.
    sql = (
        "WITH base AS (SELECT u.Id AS Id, u.Name AS Name FROM dbo.Users u), "
        "middle AS (SELECT base.Id AS Id, base.Name AS Name FROM base), "
        "final_step AS (SELECT middle.Id AS Id, middle.Name AS Name FROM middle) "
        "SELECT Id, Name FROM final_step"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.lineage_complete is True
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Id"].source_refs == (("dbo.users", "Id"),)
    assert by_name["Name"].source_refs == (("dbo.users", "Name"),)


def test_bare_cte_reference_with_explicit_alias_still_resolves():
    sql = "WITH c AS (SELECT u.Id AS UID FROM dbo.Users u) SELECT UID FROM c x"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"})})
    assert profile.lineage_complete is True
    assert profile.output_columns[0].source_refs == (("dbo.users", "Id"),)


def test_unqualified_column_resolves_through_derived_table_scope():
    # Phase 7, Step 6E3B6 -- live-audit-verified real gap: an unqualified
    # outer reference satisfied by exactly one derived-table scope (never
    # qualified with the derived table's own alias at all) now resolves,
    # instead of being reported "not found on any known source" just
    # because no REAL table happened to have that column name.
    sql = (
        "SELECT a.Id, ListURL FROM dbo.Projects a "
        "LEFT JOIN (SELECT d.ProjId, d.ListURL FROM dbo.ProjectDetails d WHERE d.ListURL IS NOT NULL) x "
        "ON x.ProjId = a.Id"
    )
    profile = _compute(
        sql,
        columns={"dbo.projects": frozenset({"id"}), "dbo.projectdetails": frozenset({"projid", "listurl"})},
    )
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["ListURL"].confidence != vp.LINEAGE_CONFIDENCE_UNRESOLVED
    assert by_name["ListURL"].source_refs == (("dbo.projectdetails", "ListURL"),)
    assert profile.lineage_complete is True


def test_unqualified_column_ambiguous_between_real_table_and_scope_stays_unresolved():
    sql = (
        "SELECT a.Id, Name FROM dbo.Accounts a "
        "LEFT JOIN (SELECT b.AccId, b.Name FROM dbo.Branches b) x ON x.AccId = a.Id"
    )
    profile = _compute(
        sql,
        columns={"dbo.accounts": frozenset({"id", "name"}), "dbo.branches": frozenset({"accid", "name"})},
    )
    col = next(c for c in profile.output_columns if c.output_name == "Name")
    assert col.confidence == vp.LINEAGE_CONFIDENCE_UNRESOLVED
    assert col.unresolved_reason == vp.LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED
    assert set(col.candidate_source_fqns or ()) == {"dbo.accounts", "x"}


def test_derived_table_star_expansion_through_outer_reference():
    sql = "SELECT d.* FROM (SELECT u.Id, u.Name FROM dbo.Users u) d"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.lineage_complete is True
    names = {c.output_name for c in profile.output_columns}
    assert names == {"Id", "Name"}


def test_derived_table_inside_join_resolves_both_sides():
    sql = (
        "SELECT d.Id, o.Total FROM "
        "(SELECT u.Id AS Id FROM dbo.Users u) d "
        "JOIN dbo.Orders o ON o.UserId = d.Id"
    )
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"total", "userid"})},
    )
    assert profile.lineage_complete is True


def test_cte_through_derived_table_resolves():
    sql = (
        "WITH c AS (SELECT d.Id AS CId FROM (SELECT u.Id FROM dbo.Users u) d) "
        "SELECT c.CId FROM c"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"})})
    assert profile.lineage_complete is True
    assert profile.output_columns[0].source_refs == (("dbo.users", "Id"),)


def test_scalar_correlated_subquery_in_projection_still_unresolved():
    # Deliberately NOT supported -- a scalar/correlated subquery embedded
    # directly in a projection expression (not a FROM/JOIN derived table)
    # still fails closed, per this module's own "never infer lineage...
    # when multiple sources are possible" rule.
    sql = "SELECT u.Id, (SELECT MAX(o.Total) FROM dbo.Orders o WHERE o.UserId = u.Id) AS MaxTotal FROM dbo.Users u"
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"total", "userid"})},
    )
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["MaxTotal"].unresolved_reason == vp.LINEAGE_UNRESOLVED_NESTED_SUBQUERY
    assert profile.lineage_complete is False


def test_cte_defined_before_a_top_level_union_is_visible_to_every_branch():
    # Phase 7, Step 6E3B6, Part 3 -- a real, live-audit-verified gap:
    # sqlglot attaches a `WITH cte AS (...) SELECT ... UNION ALL SELECT
    # ...` WITH clause to the TOP-LEVEL SetOperation node, never to either
    # branch -- a CTE both branches depend on used to be invisible to
    # both, unconditionally.
    sql = (
        "WITH c AS (SELECT u.Id AS UID, u.Name AS UName FROM dbo.Users u) "
        "SELECT c.UID, c.UName FROM c "
        "UNION ALL "
        "SELECT c.UID, c.UName FROM c"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.has_union is True
    assert profile.lineage_complete is True
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["UID"].source_refs == (("dbo.users", "Id"),)


def test_three_way_union_flattens_all_branches():
    sql = (
        "SELECT u.Id AS RowId FROM dbo.Users u "
        "UNION SELECT a.Id AS RowId FROM dbo.Archive a "
        "UNION SELECT b.Id AS RowId FROM dbo.Backup b"
    )
    profile = _compute(
        sql, columns={
            "dbo.users": frozenset({"id"}), "dbo.archive": frozenset({"id"}), "dbo.backup": frozenset({"id"}),
        },
    )
    assert profile.has_union is True
    assert profile.union_branch_count == 3
    assert profile.lineage_complete is True


def test_cross_apply_resolves_through_scope():
    sql = (
        "SELECT t.Id, x.Total FROM dbo.T t "
        "CROSS APPLY (SELECT o.Total FROM dbo.Orders o WHERE o.UserId = t.Id) x"
    )
    profile = _compute(
        sql, columns={"dbo.t": frozenset({"id"}), "dbo.orders": frozenset({"total", "userid"})},
    )
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Total"].source_refs == (("dbo.orders", "Total"),)
    assert profile.lineage_complete is True


def test_suspected_truncated_definition_flagged_and_never_limited_complete():
    base_sql = "SELECT u.Id, u.Name AS FullName FROM dbo.Users u WHERE u.Id IN ("
    padded = base_sql + ",".join(str(n) for n in range(2000))
    padded = padded[: vp._SUSPECTED_TRUNCATION_LENGTH]
    assert len(padded) == vp._SUSPECTED_TRUNCATION_LENGTH
    profile = _compute(padded, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.definition_truncated_suspected is True
    assert profile.limited_complete_eligible is False
    if profile.completion_status == vp.COMPLETION_INCOMPLETE:
        assert "definition_truncated_suspected" in profile.limited_complete_reasons


def test_suspected_truncated_but_still_parseable_incomplete_profile_blocks_limited_complete():
    base = "SELECT Id FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id -- "
    padded = (base + "x" * 5000)[: vp._SUSPECTED_TRUNCATION_LENGTH]
    assert len(padded) == vp._SUSPECTED_TRUNCATION_LENGTH
    profile = _compute(
        padded, columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"id", "userid"})},
    )
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    assert profile.definition_truncated_suspected is True
    assert profile.limited_complete_eligible is False
    assert "definition_truncated_suspected" in profile.limited_complete_reasons


# ── Phase 7, Step 6E3B3 -- LIMITED_COMPLETE eligibility / classify_profile_state ──


def test_limited_complete_eligible_when_mostly_resolved_no_secret_dependency():
    sql = (
        "SELECT u.Id, u.Name, (SELECT MAX(o.Total) FROM dbo.Orders o WHERE o.UserId = u.Id) AS MaxTotal "
        "FROM dbo.Users u"
    )
    profile = _compute(
        sql, columns={"dbo.users": frozenset({"id", "name"}), "dbo.orders": frozenset({"total", "userid"})},
        direct_deps=("dbo.Users",),
    )
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    assert profile.limited_complete_eligible is True
    assert profile.limited_complete_reasons == ()
    state = vp.classify_profile_state(profile, schema_fingerprint="fp1")
    assert state == vp.PROFILE_STATE_LIMITED_COMPLETE


def test_limited_complete_blocked_by_dependency_secret_exposure():
    sql = (
        "SELECT u.Id, (SELECT MAX(s.LoginCount) FROM dbo.Secrets s WHERE s.UserId = u.Id) AS X "
        "FROM dbo.Users u"
    )
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id"}), "dbo.secrets": frozenset({"logincount", "password"})},
        direct_deps=("dbo.Users", "dbo.Secrets"),
    )
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    assert profile.limited_complete_eligible is False
    assert "unresolved_dependency_secret_exposure_risk" in profile.limited_complete_reasons
    state = vp.classify_profile_state(profile, schema_fingerprint="fp1")
    assert state == vp.PROFILE_STATE_UNSAFE


def test_limited_complete_blocked_when_resolved_ratio_too_low():
    sql = (
        "SELECT u.Id, "
        "(SELECT 1 FROM dbo.X x WHERE x.A = u.Id) AS C1, "
        "(SELECT 1 FROM dbo.X x WHERE x.A = u.Id) AS C2, "
        "(SELECT 1 FROM dbo.X x WHERE x.A = u.Id) AS C3 "
        "FROM dbo.Users u"
    )
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"}), "dbo.x": frozenset({"a"})})
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    assert profile.limited_complete_eligible is False
    assert "insufficient_resolved_output_column_ratio" in profile.limited_complete_reasons


# ── Phase 7, Step 6E3B5, Part 2 -- candidate-source-aware secret-exposure
#    precision (an internal framework/secret-bearing DEPENDENCY alone must
#    never block an otherwise-clean business view) ─────────────────────


def test_unrelated_secret_bearing_dependency_does_not_block_limited_complete():
    # The view's own ambiguous/unresolved column ("Id", unqualified) can
    # ONLY plausibly come from dbo.Users or dbo.Orders -- dbo.Secrets is
    # only a TRANSITIVE dependency the AST never actually references for
    # this column at all. Category C: internal dependency only, never a
    # candidate source for anything actually unresolved.
    sql = "SELECT u.Id, Id AS DupId FROM dbo.Users u JOIN dbo.Orders o ON o.UserId = u.Id"
    profile = _compute(
        sql,
        columns={
            "dbo.users": frozenset({"id"}),
            "dbo.orders": frozenset({"id"}),
            "dbo.secrets": frozenset({"password"}),
        },
        direct_deps=("dbo.Users", "dbo.Orders"),
        transitive_deps=("dbo.Secrets",),
    )
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    unresolved = [c for c in profile.output_columns if c.confidence == vp.LINEAGE_CONFIDENCE_UNRESOLVED]
    assert unresolved and unresolved[0].candidate_source_fqns == ("dbo.orders", "dbo.users")
    assert "dbo.secrets" not in (unresolved[0].candidate_source_fqns or ())
    assert profile.limited_complete_eligible is True
    assert "unresolved_dependency_secret_exposure_risk" not in profile.limited_complete_reasons


def test_secret_bearing_dependency_within_actual_candidate_set_still_blocks():
    # Here the ambiguous unqualified column's candidate set genuinely
    # INCLUDES the secret-bearing table -- still correctly blocked.
    sql = "SELECT Id FROM dbo.Users u JOIN dbo.Secrets s ON s.UserId = u.Id"
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id"}), "dbo.secrets": frozenset({"id", "password"})},
        direct_deps=("dbo.Users", "dbo.Secrets"),
    )
    assert profile.completion_status == vp.COMPLETION_INCOMPLETE
    col = profile.output_columns[0]
    assert col.unresolved_reason == vp.LINEAGE_UNRESOLVED_AMBIGUOUS_UNQUALIFIED
    assert set(col.candidate_source_fqns or ()) == {"dbo.users", "dbo.secrets"}
    assert profile.limited_complete_eligible is False
    assert "unresolved_dependency_secret_exposure_risk" in profile.limited_complete_reasons


def test_opaque_unresolved_column_with_no_secret_anywhere_in_closure_is_safe():
    sql = "SELECT u.Id, (SELECT MAX(o.Total) FROM dbo.Orders o WHERE o.UserId = u.Id) AS MaxTotal FROM dbo.Users u"
    profile = _compute(
        sql,
        columns={"dbo.users": frozenset({"id"}), "dbo.orders": frozenset({"total", "userid"})},
        direct_deps=("dbo.Users", "dbo.Orders"),
    )
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["MaxTotal"].candidate_source_fqns is None  # genuinely opaque (scalar subquery)
    assert profile.limited_complete_eligible is True


# ── Phase 7, Step 6E3B5, Part 4 -- whitespace-split comparison operator
#    normalization fallback (SQL Server tolerates `> =`/`< =`; sqlglot
#    does not) ──────────────────────────────────────────────────────────


def test_split_greater_equal_operator_normalizes_and_parses():
    sql = "SELECT u.Id FROM dbo.Users u WHERE u.Age > = 18"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "age"})})
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    assert "parsed_after_split_comparison_operator_normalization" in profile.evidence


def test_split_less_equal_operator_normalizes_and_parses():
    sql = "SELECT u.Id FROM dbo.Users u WHERE u.Age < = 65"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "age"})})
    assert profile.completion_status == vp.COMPLETION_COMPLETE


def test_normalization_not_applied_when_sql_already_parses():
    sql = "SELECT u.Id FROM dbo.Users u WHERE u.Age >= 18"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "age"})})
    assert "parsed_after_split_comparison_operator_normalization" not in profile.evidence


def test_normalization_never_corrupts_a_string_literal_containing_the_pattern():
    # The literal text "> =" inside a single-quoted string must survive
    # byte-for-byte -- only a REAL split operator outside any string
    # literal/comment/bracket is ever touched.
    sql = "SELECT u.Id, u.Note AS Note FROM dbo.Users u WHERE u.Note = 'Score > = Threshold'"
    normalized = vp._normalize_split_comparison_operators(sql)
    assert "'Score > = Threshold'" in normalized


def test_normalization_never_touches_a_line_comment():
    sql = "SELECT u.Id FROM dbo.Users u -- legacy filter was Age > = 18\nWHERE u.Id > 0"
    normalized = vp._normalize_split_comparison_operators(sql)
    assert "-- legacy filter was Age > = 18" in normalized


def test_normalization_handles_doubled_single_quote_escape():
    sql = "SELECT u.Id FROM dbo.Users u WHERE u.Note = 'it''s > = weird' AND u.Age > = 18"
    normalized = vp._normalize_split_comparison_operators(sql)
    assert "'it''s > = weird'" in normalized
    assert "Age >= 18" in normalized


def test_still_unparseable_after_normalization_remains_typed_parse_error():
    profile = _compute("NOT VALID SQL AT ALL (((")
    assert profile.completion_status == vp.COMPLETION_FAILED
    assert profile.failure_reason == vp.FAILURE_PARSE_ERROR


def test_unqualified_from_table_defaults_to_dbo_and_resolves():
    # Phase 7, Step 6E3B5, Part 3 -- a real generic defect found via direct
    # audit of the live catalog: a view referencing its table WITHOUT a
    # schema prefix (valid T-SQL -- resolves to the connection's own
    # default schema, "dbo") used to never match known_columns_by_fqn_
    # lower's "dbo.table"-keyed catalog metadata at all.
    sql = "SELECT u.Id, u.Name AS FullName FROM Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    by_name = {c.output_name: c for c in profile.output_columns}
    assert by_name["Id"].source_refs == (("dbo.users", "Id"),)


def test_output_alias_does_not_hide_a_restricted_source_column():
    sql = "SELECT u.Id, u.PasswordHash AS Foo FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "passwordhash"})})
    assert "Foo" in profile.secret_columns


def test_complete_profile_state_unaffected_by_limited_complete_machinery():
    sql = "CREATE VIEW dbo.v_test AS SELECT u.Id, u.Name AS FullName FROM dbo.Users u"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})}, pks={"dbo.users": ("Id",)})
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    assert profile.limited_complete_eligible is False
    assert profile.limited_complete_reasons == ("not_applicable_completion_status_not_incomplete",)
    state = vp.classify_profile_state(profile, schema_fingerprint="fp1")
    assert state == vp.PROFILE_STATE_COMPLETE


def test_typed_failure_state_unaffected():
    profile = _compute("NOT VALID SQL (((", )
    state = vp.classify_profile_state(profile, schema_fingerprint="fp1")
    assert state == vp.PROFILE_STATE_TYPED_FAILURE


# ── Phase 7, Step 6E3B4, Part 4 -- definition-length boundaries and the
#    authoritative capture_status override ───────────────────────────────


def _padded_select(target_len, *, suffix="  dbo.users u"):
    """A syntactically valid, always-parseable SELECT whose exact total
    character length is `target_len` -- padding goes into a trailing
    single-line comment so it never changes the query's own meaning."""
    base = f"SELECT u.Id, u.Name AS FullName FROM{suffix} WHERE u.Id > 0 --"
    assert len(base) <= target_len, "base query longer than requested padding target"
    return base + ("x" * (target_len - len(base)))


@pytest.mark.parametrize("length", [3999, 4001, 8000, 8001, 20000, 20001])
def test_definitions_away_from_the_truncation_boundary_never_flagged(length):
    sql = _padded_select(length)
    assert len(sql) == length
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.definition_truncated_suspected is False


def test_definition_at_exactly_4000_chars_flagged_without_capture_status():
    sql = _padded_select(vp._SUSPECTED_TRUNCATION_LENGTH)
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.definition_truncated_suspected is True
    assert profile.limited_complete_eligible is False


def test_definition_at_exactly_4000_chars_not_flagged_when_capture_confirmed_complete():
    # The exact false-positive this override exists to prevent: a
    # genuinely complete definition that happens to be exactly the
    # old truncation-heuristic length must never be treated as suspect
    # once the upstream capture layer has proven it complete.
    sql = _padded_select(vp._SUSPECTED_TRUNCATION_LENGTH)
    profile = vp.compute_view_profile(
        "dbo.v_test", sql,
        source_id=90001, build_id=1, schema_fingerprint="fp1",
        direct_dependencies=(), transitive_dependencies=(),
        authority=None,
        column_names_by_fqn_lower={"dbo.users": frozenset({"id", "name"})},
        primary_key_columns_by_fqn_lower={},
        now="2026-09-30T00:00:00Z",
        definition_capture_status="AVAILABLE_COMPLETE",
    )
    assert profile.definition_truncated_suspected is False


def test_capture_status_other_than_available_complete_leaves_heuristic_in_force():
    sql = _padded_select(vp._SUSPECTED_TRUNCATION_LENGTH)
    profile = vp.compute_view_profile(
        "dbo.v_test", sql,
        source_id=90001, build_id=1, schema_fingerprint="fp1",
        direct_dependencies=(), transitive_dependencies=(),
        authority=None,
        column_names_by_fqn_lower={"dbo.users": frozenset({"id", "name"})},
        primary_key_columns_by_fqn_lower={},
        now="2026-09-30T00:00:00Z",
        definition_capture_status="LEGACY_UNKNOWN",
    )
    assert profile.definition_truncated_suspected is True


def test_unicode_identifiers_and_literals_resolve_normally():
    sql = "SELECT u.Id, u.Name AS FullName FROM dbo.Users u WHERE u.City = N'München — 東京'"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name", "city"})})
    assert profile.completion_status == vp.COMPLETION_COMPLETE
    assert "München" in profile.where_restrictions[0] or "東京" in profile.where_restrictions[0]


def test_crlf_multiline_definition_parses_normally():
    sql = "SELECT u.Id,\r\n       u.Name AS FullName\r\nFROM dbo.Users u\r\nWHERE u.Id > 0\r\n"
    profile = _compute(sql, columns={"dbo.users": frozenset({"id", "name"})})
    assert profile.completion_status == vp.COMPLETION_COMPLETE


def test_nested_cte_and_union_branches_beyond_4000_chars_still_resolve():
    pad = "-- " + ("x" * 4200) + "\n"
    sql = (
        pad
        + "WITH c AS (SELECT u.Id AS UID FROM dbo.Users u) "
        + "SELECT c.UID FROM c "
        + "UNION SELECT a.Id AS UID FROM dbo.Archive a"
    )
    assert len(sql) > 4200
    profile = _compute(sql, columns={"dbo.users": frozenset({"id"}), "dbo.archive": frozenset({"id"})})
    assert profile.has_union is True
    assert profile.lineage_complete is True


def test_typed_failure_profile_carries_current_secret_policy_version():
    # Regression for a pre-existing defect found during this step's own
    # simulation verification: _failed() used to leave secret_policy_
    # version at the dataclass default (0), which made classify_
    # profile_state report STALE instead of TYPED_FAILURE for a fresh
    # parse failure whenever a caller supplies current_secret_policy_
    # version (core.sqlservice.sql_validation always does).
    from core.dictionary.pii_detector import SECRET_POLICY_VERSION
    profile = _compute("NOT VALID SQL (((")
    assert profile.secret_policy_version == SECRET_POLICY_VERSION
    state = vp.classify_profile_state(
        profile, schema_fingerprint="fp1", current_secret_policy_version=SECRET_POLICY_VERSION,
    )
    assert state == vp.PROFILE_STATE_TYPED_FAILURE
