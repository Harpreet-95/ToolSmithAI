"""
NEXT MILESTONE, Part 12/13/26 — Data Quality Observation model tests.

No database, no live connection: detect_data_quality_observations() is a
pure function over hand-built table_contexts dicts shaped exactly like
data.query_planning_service.get_table_business_context's own output.
"""
from __future__ import annotations

from core.answering.data_quality_observations import (
    BusinessColumnRef,
    detect_data_quality_observations,
    executive_data_notes,
    orphan_foreign_key_observation,
    technical_diagnostics,
)


def _ctx(table_fqn, column_name, *, null_percentage=None, uniqueness_score=None, populated_count=1000):
    return {
        table_fqn: {
            "columns": [{
                "column_name": column_name,
                "profiling": {
                    "null_percentage": null_percentage,
                    "uniqueness_score": uniqueness_score,
                    "populated_count": populated_count,
                },
                "dictionary": {},
            }],
        },
    }


def test_null_business_label_flagged_when_used_as_display_dimension():
    # "How many classes are there by location?" — City is the label being
    # displayed, and it's NULL for a material fraction of rows.
    ctx = _ctx("dbo.EDU_Location", "City", null_percentage=18.0, populated_count=500)
    refs = [BusinessColumnRef("dbo.EDU_Location", "City", role="label", business_concept="Location")]

    obs = detect_data_quality_observations(ctx, refs)

    assert len(obs) == 1
    assert obs[0].issue_type == "NULL_BUSINESS_LABEL"
    assert obs[0].severity == "MEDIUM"
    assert obs[0].affected_rows == 90  # 500 * 18%
    assert "18.0%" in obs[0].evidence
    assert "City" not in obs[0].message  # plain business_concept wording, not raw column name


def test_label_null_rate_below_threshold_is_not_flagged():
    # A label column with only a trivial null rate is not a data-quality
    # issue — Part 13: not every null is an error.
    ctx = _ctx("dbo.EDU_Location", "City", null_percentage=1.0, populated_count=500)
    refs = [BusinessColumnRef("dbo.EDU_Location", "City", role="label")]

    obs = detect_data_quality_observations(ctx, refs)

    assert obs == []


def test_optional_column_not_in_business_columns_never_flagged():
    # A column with a high null rate that the caller never named as
    # business-significant (e.g. an unrelated "Notes" field) must never be
    # inspected at all — only columns explicitly passed in business_columns
    # are ever looked at.
    ctx = _ctx("dbo.EDU_Class", "Notes", null_percentage=95.0, populated_count=500)

    obs = detect_data_quality_observations(ctx, business_columns=[])

    assert obs == []


def test_duplicate_business_key_flagged():
    ctx = _ctx("dbo.EDU_Student", "Email", uniqueness_score=0.92, populated_count=2000)
    refs = [BusinessColumnRef("dbo.EDU_Student", "Email", role="key", business_concept="Student")]

    obs = detect_data_quality_observations(ctx, refs)

    assert len(obs) == 1
    assert obs[0].issue_type == "DUPLICATE_BUSINESS_KEY"
    assert obs[0].severity == "HIGH"
    assert obs[0].affected_rows == 160  # 2000 * (1 - 0.92)


def test_unique_key_not_flagged():
    ctx = _ctx("dbo.EDU_Student", "StudentID", uniqueness_score=1.0, populated_count=2000)
    refs = [BusinessColumnRef("dbo.EDU_Student", "StudentID", role="key")]

    obs = detect_data_quality_observations(ctx, refs)

    assert obs == []


def test_orphan_foreign_key_observation_built_from_bounded_check():
    obs = orphan_foreign_key_observation(
        table_fqn="dbo.EDU_ClassSignups", column_name="InstructorID",
        related_table_fqn="dbo.EDU_Instructor", unmatched_count=42, total_count=2000,
        business_concept="Instructor",
    )
    assert obs is not None
    assert obs.issue_type == "ORPHAN_FOREIGN_KEY"
    assert obs.affected_rows == 42
    assert obs.severity == "MEDIUM"  # 2.1% -> between 1% and 10%


def test_orphan_foreign_key_no_unmatched_rows_returns_none():
    # Zero unmatched is a strength, not an issue — must not fabricate a
    # warning where the relationship is actually clean.
    obs = orphan_foreign_key_observation(
        table_fqn="dbo.EDU_ClassSignups", column_name="InstructorID",
        related_table_fqn="dbo.EDU_Instructor", unmatched_count=0, total_count=2000,
    )
    assert obs is None


def test_executive_notes_are_plain_language_only():
    ctx = _ctx("dbo.EDU_Location", "City", null_percentage=18.0, populated_count=500)
    refs = [BusinessColumnRef("dbo.EDU_Location", "City", role="label", business_concept="Location")]
    obs = detect_data_quality_observations(ctx, refs)

    notes = executive_data_notes(obs)

    assert len(notes) == 1
    assert "dbo.EDU_Location" not in notes[0]
    assert "null_percentage" not in notes[0]


def test_low_severity_observations_excluded_from_executive_notes():
    low = orphan_foreign_key_observation(
        table_fqn="t", column_name="c", related_table_fqn="r",
        unmatched_count=1, total_count=2000,  # 0.05% -> LOW
    )
    assert low.severity == "LOW"
    assert executive_data_notes([low]) == []
    assert technical_diagnostics([low]) == [{
        "issue_type": "ORPHAN_FOREIGN_KEY", "severity": "LOW",
        "table_fqn": "t", "column_name": "c", "business_concept": None,
        "evidence": low.evidence, "affected_rows": 1, "message": low.message,
    }]
