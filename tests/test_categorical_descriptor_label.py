"""
37-GATE CORRECTNESS REPAIR MILESTONE (Final Two Correctness Blockers), G-02 —
data.query_planning_service._resolve_categorical_descriptor_label /
_is_identifier_shaped_descriptor_candidate.

Covers the live-confirmed real-the reference database regression: "classes by location" grouped
by dbo.EDU_Location.State (2 distinct values, collapsing 4 real locations
into fewer buckets) instead of .City (4 distinct values) because City's
profiled uniqueness_score is computed against POPULATED rows (4 distinct / 4
populated = 1.0, indistinguishable by ratio alone from a true per-row
identifier) on a table too small for that ratio to mean anything. Also covers
the generic identifier/address guard (name-shape, value-shape, absolute
cardinality) that must keep rejecting genuinely identifier-shaped columns
even once the ratio check is relaxed for small tables.

Run:
    python -m pytest tests/test_categorical_descriptor_label.py -v
"""
from __future__ import annotations

from data.query_planning_service import (
    _is_identifier_shaped_descriptor_candidate,
    _resolve_categorical_descriptor_label,
)


def _col(column_name, *, distinct_count=None, null_percentage=0.0,
         uniqueness_score=None, avg_length=None, data_type="TEXT", **extra_profiling):
    profiling = {
        "distinct_count": distinct_count,
        "null_percentage": null_percentage,
        "uniqueness_score": uniqueness_score,
        "avg_length": avg_length,
    }
    profiling.update(extra_profiling)
    return {
        "column_name": column_name,
        "schema": {"data_type": data_type},
        "dictionary": {},
        "profiling": profiling,
    }


# ---------------------------------------------------------------------------
# The live-confirmed real-the reference database regression itself.
# ---------------------------------------------------------------------------

def test_small_table_prefers_higher_cardinality_category_over_near_unique_ratio():
    # dbo.EDU_Location, live-confirmed: 5 rows, City 4 distinct/4 populated
    # (uniqueness 1.0), State 2 distinct/4 populated (uniqueness 0.5),
    # Location_1/Location_2 genuinely address-shaped.
    columns = [
        _col("City", distinct_count=4, null_percentage=20.0, uniqueness_score=1.0, avg_length=7.0),
        _col("State", distinct_count=2, null_percentage=20.0, uniqueness_score=0.5, avg_length=2.0),
        _col("Country", distinct_count=1, null_percentage=0.0, uniqueness_score=0.2, avg_length=3.0),
        _col("Location_1", distinct_count=5, null_percentage=0.0, uniqueness_score=1.0, avg_length=16.6),
        _col("Location_2", distinct_count=2, null_percentage=60.0, uniqueness_score=1.0, avg_length=11.5),
    ]
    result = _resolve_categorical_descriptor_label("dbo.EDU_Location", columns)
    assert result is not None
    assert result["column_name"] == "City"
    assert result["source"] == "categorical_descriptor_label"


def test_address_shaped_column_never_wins_even_with_small_distinct_count():
    # Location_1 alone (no City/State competing) -- must still be rejected,
    # not fall through as "the only candidate standing."
    columns = [
        _col("Location_1", distinct_count=5, null_percentage=0.0, uniqueness_score=1.0, avg_length=16.6),
    ]
    assert _resolve_categorical_descriptor_label("dbo.EDU_Location", columns) is None


def test_ordinal_suffix_name_rejected_regardless_of_stats():
    # Same shape as Location_1/Location_2 but with statistics that would
    # otherwise easily qualify (short values, low uniqueness) -- the name
    # shape alone (core token + bare numeric suffix) must still exclude it.
    columns = [
        _col("Address_1", distinct_count=3, null_percentage=0.0, uniqueness_score=0.3, avg_length=5.0),
    ]
    assert _resolve_categorical_descriptor_label("dbo.Contacts", columns) is None


# ---------------------------------------------------------------------------
# Generic identifier/address/code guard -- must not turn every near-unique
# text column into a label just because the ratio check was relaxed.
# ---------------------------------------------------------------------------

def test_large_table_near_unique_column_still_rejected():
    # A big table (thousands of rows) with a near-unique text column (e.g.
    # free-text notes/emails) must stay excluded -- the small-cardinality
    # exemption only applies below _SMALL_CARDINALITY_EXEMPT.
    columns = [
        _col("Notes", distinct_count=4800, null_percentage=5.0, uniqueness_score=0.97, avg_length=40.0),
    ]
    assert _resolve_categorical_descriptor_label("dbo.BigTable", columns) is None


def test_identifier_name_token_rejected_even_with_low_cardinality():
    columns = [
        _col("StatusCode", distinct_count=4, null_percentage=0.0, uniqueness_score=0.8, avg_length=4.0),
    ]
    assert _resolve_categorical_descriptor_label("dbo.Orders", columns) is None


def test_value_shape_evidence_rejects_identifier_even_with_generic_name():
    # A generically-named column (no id/code/email token, no ordinal
    # suffix) whose SAMPLED VALUES are GUID-shaped must still be excluded.
    columns = [
        _col("Reference", distinct_count=8, null_percentage=0.0, uniqueness_score=0.9,
             avg_length=36.0, guid_match_rate=0.95),
    ]
    assert _resolve_categorical_descriptor_label("dbo.Orders", columns) is None


def test_absolute_cardinality_ceiling_rejects_even_without_ratio_evidence():
    # Extremely high distinct_count with NO uniqueness_score computed at all
    # -- the absolute ceiling must still exclude it (never treated as a
    # bounded business vocabulary just because ratio evidence is missing).
    columns = [
        _col("FreeText", distinct_count=50000, null_percentage=0.0, uniqueness_score=None, avg_length=8.0),
    ]
    assert _resolve_categorical_descriptor_label("dbo.BigTable", columns) is None


def test_genuine_tie_at_max_distinct_returns_none():
    columns = [
        _col("Category", distinct_count=5, null_percentage=0.0, uniqueness_score=0.6, avg_length=6.0),
        _col("Segment", distinct_count=5, null_percentage=0.0, uniqueness_score=0.6, avg_length=6.0),
    ]
    assert _resolve_categorical_descriptor_label("dbo.Widgets", columns) is None


def test_non_text_column_excluded():
    columns = [
        _col("StatusFlag", distinct_count=3, null_percentage=0.0, uniqueness_score=0.5,
             avg_length=None, data_type="INTEGER"),
    ]
    assert _resolve_categorical_descriptor_label("dbo.Orders", columns) is None


def test_no_profiling_evidence_returns_none():
    columns = [_col("Category", distinct_count=None)]
    assert _resolve_categorical_descriptor_label("dbo.Widgets", columns) is None


# ---------------------------------------------------------------------------
# _is_identifier_shaped_descriptor_candidate -- pure unit checks.
# ---------------------------------------------------------------------------

def test_identifier_shape_true_for_ordinal_suffix_name():
    assert _is_identifier_shaped_descriptor_candidate(_col("Location_1")) is True


def test_identifier_shape_true_for_identifier_name_token():
    assert _is_identifier_shaped_descriptor_candidate(_col("ContactEmail")) is True


def test_identifier_shape_true_for_long_avg_length():
    assert _is_identifier_shaped_descriptor_candidate(_col("Blob", avg_length=200.0)) is True


def test_identifier_shape_true_for_value_shape_rate():
    assert _is_identifier_shaped_descriptor_candidate(_col("Reference", phone_match_rate=0.9)) is True


def test_identifier_shape_false_for_plain_category_column():
    assert _is_identifier_shaped_descriptor_candidate(_col("City", avg_length=7.0)) is False
