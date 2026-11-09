"""
Focused unit tests -- SQL Architecture Correction, Phase 6, Step 6C, Part 2:
deterministic VIEW-ONLY candidate selection
(core/sqlservice/view_semantic_enrichment_selection.py). Pure/offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.sqlservice.view_semantic_enrichment_selection import (
    EXCLUSION_REASON_INELIGIBLE_TIER,
    ViewEnrichmentCandidate,
    ViewSelectionResult,
    select_view_enrichment_candidates,
)


@dataclass
class _Doc:
    object_fqn: str
    object_type: str
    retrieval_tier: int
    metadata: dict = field(default_factory=dict)


def test_eligible_tier1_view_selected():
    doc = _Doc("dbo.ActiveStudents", "VIEW", 1, {"schema_name": "dbo", "table_name": "ActiveStudents"})
    result = select_view_enrichment_candidates([doc])
    assert result.selected_count == 1
    assert result.candidates[0] == ViewEnrichmentCandidate(
        object_fqn="dbo.ActiveStudents", schema_name="dbo", view_name="ActiveStudents", retrieval_tier=1,
    )


def test_eligible_tier2_view_selected():
    doc = _Doc("dbo.EnrollmentSummary", "VIEW", 2, {"schema_name": "dbo", "table_name": "EnrollmentSummary"})
    result = select_view_enrichment_candidates([doc])
    assert result.selected_count == 1
    assert result.candidates[0].retrieval_tier == 2


def test_table_excluded_entirely():
    table_doc = _Doc("dbo.Students", "TABLE", 1, {"schema_name": "dbo", "table_name": "Students"})
    view_doc = _Doc("dbo.V1", "VIEW", 1, {"schema_name": "dbo", "table_name": "V1"})
    result = select_view_enrichment_candidates([table_doc, view_doc])
    assert result.selected_count == 1
    assert result.candidates[0].object_fqn == "dbo.V1"
    # the TABLE is not even counted as an excluded view -- it is simply absent
    assert result.total_documents == 1
    assert all(e.object_fqn != "dbo.Students" for e in result.excluded)


@pytest.mark.parametrize("tier", [3, 4])
def test_tier_3_4_view_excluded(tier):
    doc = _Doc("dbo.ArchiveView", "VIEW", tier, {"schema_name": "dbo", "table_name": "ArchiveView"})
    result = select_view_enrichment_candidates([doc])
    assert result.selected_count == 0
    assert len(result.excluded) == 1
    assert result.excluded[0].object_fqn == "dbo.ArchiveView"
    assert result.excluded[0].reason == EXCLUSION_REASON_INELIGIBLE_TIER


def test_archive_system_view_excluded_via_tier_reuse():
    """This module never re-implements the archive/system/backup/staging/
    temp/test/deprecated exclusion vocabulary -- it trusts retrieval_tier
    (already classified upstream by core.sqlservice.retrieval_documents.
    classify_retrieval_tier, which assigns exactly these categories to Tier
    3/4). A caller passing a document already tier-classified as 4 for
    being e.g. "vw_Students_Archive" is excluded purely via the tier check,
    proving the reuse boundary end to end."""
    doc = _Doc("dbo.vw_Students_Archive", "VIEW", 4, {"schema_name": "dbo", "table_name": "vw_Students_Archive"})
    result = select_view_enrichment_candidates([doc])
    assert result.selected_count == 0
    assert result.excluded[0].reason == EXCLUSION_REASON_INELIGIBLE_TIER


def test_deterministic_ordering_by_canonical_fqn():
    docs = [
        _Doc("dbo.Zeta", "VIEW", 1, {"schema_name": "dbo", "table_name": "Zeta"}),
        _Doc("dbo.Alpha", "VIEW", 1, {"schema_name": "dbo", "table_name": "Alpha"}),
        _Doc("dbo.mid", "VIEW", 2, {"schema_name": "dbo", "table_name": "mid"}),
    ]
    result = select_view_enrichment_candidates(docs)
    assert [c.object_fqn for c in result.candidates] == ["dbo.Alpha", "dbo.mid", "dbo.Zeta"]

    # Reordering the input never changes the output order.
    result2 = select_view_enrichment_candidates(list(reversed(docs)))
    assert result.candidates == result2.candidates


def test_strict_configurable_maximum_truncates_deterministically():
    docs = [_Doc(f"dbo.V{i}", "VIEW", 1, {"schema_name": "dbo", "table_name": f"V{i}"}) for i in range(5)]
    result = select_view_enrichment_candidates(docs, max_total_views=3)
    assert result.selected_count == 3
    assert result.truncated_count == 2
    assert [c.object_fqn for c in result.candidates] == ["dbo.V0", "dbo.V1", "dbo.V2"]
    assert result.truncated_object_fqns == ("dbo.V3", "dbo.V4")
    assert any("truncated" in w for w in result.warnings)


def test_max_total_views_must_be_positive():
    with pytest.raises(ValueError):
        select_view_enrichment_candidates([], max_total_views=0)


def test_immutable_result_is_a_frozen_dataclass():
    result = select_view_enrichment_candidates([])
    with pytest.raises(Exception):
        result.selected_count = 99  # type: ignore[misc]
    assert isinstance(result, ViewSelectionResult)


def test_case_insensitive_object_type_matching():
    doc = _Doc("dbo.V1", "view", 1, {"schema_name": "dbo", "table_name": "V1"})
    result = select_view_enrichment_candidates([doc])
    assert result.selected_count == 1


def test_schema_and_view_name_derived_from_fqn_when_metadata_absent():
    doc = _Doc("dbo.Fallback", "VIEW", 1, {})
    result = select_view_enrichment_candidates([doc])
    assert result.candidates[0].schema_name == "dbo"
    assert result.candidates[0].view_name == "Fallback"


def test_counts_are_internally_consistent():
    docs = [
        _Doc("dbo.T1", "TABLE", 1, {}),
        _Doc("dbo.V1", "VIEW", 1, {"schema_name": "dbo", "table_name": "V1"}),
        _Doc("dbo.V2", "VIEW", 3, {"schema_name": "dbo", "table_name": "V2"}),
    ]
    result = select_view_enrichment_candidates(docs)
    assert result.total_documents == result.eligible_view_count + result.excluded_count
    assert result.eligible_view_count == result.selected_count + result.truncated_count
