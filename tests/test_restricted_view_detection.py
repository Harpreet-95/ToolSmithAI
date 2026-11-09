"""
37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-view safety.

Pure-function tests for data.semantic_contract_service._detect_view_
restriction / _extract_lineage_root_table: the generic, no-table-names-
hardcoded scan of a VIEW's own definition text for evidence it represents a
filtered business slice (a hardcoded WHERE literal, TOP/LIMIT row-capping)
rather than the full apparent population, and for its structural FROM-clause
source. Fixture text below is dbo.vw_EDU_Enrollments's real, live-confirmed
definition (WHERE AP.TrackID IN (1000), covering ~51% of the true
dbo.EDU_ClassSignups population).
"""
import os

from cryptography.fernet import Fernet

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("JWT_SECRET", "test-37gate-restricted-view-secret-long-enough-1")
os.environ.setdefault("USER_ID_SALT", "test-37gate-restricted-view-salt-long-enough-12")

from data.semantic_contract_service import (
    _detect_view_restriction, _extract_lineage_root_table, _extract_all_referenced_tables,
)

_REAL_VIEW_DEFINITION = """
CREATE View [dbo].[vw_EDU_Enrollments]
AS
  SELECT distinct acs.ClassSignUpsID, ADC.MeetingURL, du.FirstName, du.LastName,
        adc.ClassName, adc.StartDate as ClassStartDate, acs.StartDate as EnrollmentDate
  FROM dbo.EDU_ClassSignups  acs INNER JOIN  dbo.EDU_Student ads on acs.StudentID = ads.StudentID
      INNER JOIN dnnuser.Users du ON ads.StudentUserID = du.userid
      INNER JOIN dbo.EDU_Class adc ON adc.classid = acs.classid
      INNER JOIN dbo.EDU_TRACK ap on adc.pathid = ap.pathid
  WHERE AP.TrackID IN (1000)
"""

_UNRESTRICTED_VIEW_DEFINITION = """
CREATE VIEW dbo.vw_AllStudents AS
  SELECT s.StudentID, s.FirstName, s.LastName
  FROM dbo.EDU_Student s
"""


def test_detect_view_restriction_finds_where_literal_filter():
    result = _detect_view_restriction(_REAL_VIEW_DEFINITION)
    assert result["restriction_detected"] is True
    assert "where_literal_filter" in result["restriction_kinds"]


def test_detect_view_restriction_none_for_no_definition_text():
    assert _detect_view_restriction(None) is None
    assert _detect_view_restriction("") is None


def test_detect_view_restriction_unrestricted_view_not_flagged():
    result = _detect_view_restriction(_UNRESTRICTED_VIEW_DEFINITION)
    assert result["restriction_detected"] is False


def test_detect_view_restriction_row_limit_pattern():
    definition = "CREATE VIEW dbo.vw_Top10 AS SELECT TOP (10) * FROM dbo.EDU_Class"
    result = _detect_view_restriction(definition)
    assert result["restriction_detected"] is True
    assert "row_limit" in result["restriction_kinds"]


def test_extract_lineage_root_table_finds_from_clause_root():
    root = _extract_lineage_root_table(_REAL_VIEW_DEFINITION)
    assert root == "dbo.EDU_ClassSignups"


def test_extract_lineage_root_table_none_for_no_definition_text():
    assert _extract_lineage_root_table(None) is None


def test_extract_lineage_root_table_ignores_unschema_qualified_names():
    definition = "CREATE VIEW dbo.v AS SELECT * FROM SomeUnqualifiedTable"
    assert _extract_lineage_root_table(definition) is None


# =============================================================================
# View Semantic Discovery milestone — _extract_all_referenced_tables, the
# generalization of _extract_lineage_root_table to every FROM/JOIN clause.
# =============================================================================

def test_extract_all_referenced_tables_finds_every_from_and_join():
    tables = _extract_all_referenced_tables(_REAL_VIEW_DEFINITION)
    assert "dbo.EDU_ClassSignups" in tables
    assert "dbo.EDU_Student" in tables
    assert "dbo.EDU_Class" in tables
    assert "dbo.EDU_TRACK" in tables


def test_extract_all_referenced_tables_none_for_no_definition_text():
    assert _extract_all_referenced_tables(None) == []
    assert _extract_all_referenced_tables("") == []


def test_extract_all_referenced_tables_dedupes_case_insensitively():
    definition = "CREATE VIEW dbo.v AS SELECT * FROM dbo.Foo f JOIN dbo.foo g ON f.id = g.id"
    tables = _extract_all_referenced_tables(definition)
    assert tables == ["dbo.Foo"]


def test_extract_all_referenced_tables_filters_to_known_catalog():
    definition = "CREATE VIEW dbo.v AS SELECT * FROM dbo.RealTable r JOIN dbo.NotInCatalog n ON r.id = n.id"
    tables = _extract_all_referenced_tables(definition, known_table_fqns={"dbo.RealTable"})
    assert tables == ["dbo.RealTable"]


def test_extract_all_referenced_tables_known_catalog_empty_set_excludes_everything():
    tables = _extract_all_referenced_tables(_UNRESTRICTED_VIEW_DEFINITION, known_table_fqns=set())
    assert tables == []
