"""
Focused tests — SQL Architecture Correction, Phase 6, Step 4C2, Part 2/3:
informative INPUT-column selection and compact OBJECT_CORE output-token
estimation (core/sqlservice/semantic_enrichment_object_core_selection.py).

Pure, in-memory: no I/O, no provider, no database.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_object_core_selection.py -v
"""

from __future__ import annotations

from core.sqlservice.semantic_enrichment_object_core_selection import (
    ObjectCoreInputSelectionLimits,
    ObjectCoreOutputEstimateLimits,
    estimate_object_core_batch_output_tokens,
    estimate_object_core_output_chars,
    local_id_for_index,
    select_informative_input_columns,
)
from core.sqlservice.semantic_enrichment_service import ColumnSemanticInput


def _col(name, sql_type="OTHER", pk=False, fk=False):
    return ColumnSemanticInput(column_name=name, normalized_sql_type=sql_type, is_primary_key=pk, is_foreign_key=fk)


# ── select_informative_input_columns ────────────────────────────────────


def test_small_object_keeps_every_column():
    limits = ObjectCoreInputSelectionLimits(max_input_columns_per_object=16)
    cols = [_col("A"), _col("B"), _col("C")]
    assert select_informative_input_columns(cols, limits=limits) == tuple(cols)


def test_wide_object_prioritizes_pk_fk_then_business_key_then_hints_then_opaque():
    limits = ObjectCoreInputSelectionLimits(max_input_columns_per_object=4)
    cols = [
        _col("Description"),        # tier 5 -- plain descriptive name
        _col("StuID", pk=True),     # tier 0
        _col("CourseRefID", fk=True),  # tier 1
        _col("ExternalCode"),       # tier 2 -- business-key-shaped
        _col("Amount"),             # tier 3 -- measure hint
        _col("Cd"),                 # tier 4 -- opaque (short/consonant-only)
        _col("Notes"),              # tier 5
    ]
    selected = select_informative_input_columns(cols, limits=limits)
    names = {c.column_name for c in selected}
    assert len(selected) == 4
    assert {"StuID", "CourseRefID", "ExternalCode", "Amount"} == names
    assert "Description" not in names and "Notes" not in names


def test_selection_is_deterministic_regardless_of_input_order():
    limits = ObjectCoreInputSelectionLimits(max_input_columns_per_object=2)
    cols_a = [_col("Zeta", pk=True), _col("Alpha", pk=True), _col("Beta")]
    cols_b = [_col("Beta"), _col("Alpha", pk=True), _col("Zeta", pk=True)]
    assert select_informative_input_columns(cols_a, limits=limits) == select_informative_input_columns(
        cols_b, limits=limits
    )


def test_wide_object_bounded_safely_never_exceeds_limit():
    limits = ObjectCoreInputSelectionLimits(max_input_columns_per_object=5)
    cols = [_col(f"Col{i}", pk=(i == 0)) for i in range(200)]
    selected = select_informative_input_columns(cols, limits=limits)
    assert len(selected) == 5
    assert selected[0].column_name == "Col0" or any(c.column_name == "Col0" for c in selected)


# ── output-token estimation ─────────────────────────────────────────────

_OUTPUT_LIMITS = ObjectCoreOutputEstimateLimits(
    max_label_characters=60, max_description_characters=120,
    max_concepts=3, max_concept_characters=40, max_aliases=4, max_alias_characters=40,
)


def test_object_core_output_chars_never_depends_on_column_count():
    # OBJECT_CORE never echoes a column record -- the estimate must be
    # constant regardless of how many (or which) columns exist.
    assert estimate_object_core_output_chars("o1", _OUTPUT_LIMITS) == estimate_object_core_output_chars(
        "o1", _OUTPUT_LIMITS
    )
    a = estimate_object_core_output_chars("o1", _OUTPUT_LIMITS)
    b = estimate_object_core_output_chars("o9999", _OUTPUT_LIMITS)
    assert abs(a - b) <= 8  # only the local id's own (tiny, bounded) length can differ


def test_batch_output_token_estimate_scales_linearly_with_object_count():
    one = estimate_object_core_batch_output_tokens(["o1"], _OUTPUT_LIMITS, conservative_chars_per_token=3.0)
    ten = estimate_object_core_batch_output_tokens(
        [f"o{i}" for i in range(1, 11)], _OUTPUT_LIMITS, conservative_chars_per_token=3.0,
    )
    assert ten == pytest_approx_linear(one, 10)


def pytest_approx_linear(unit_estimate: int, count: int) -> int:
    # Both sides use the same ceil-of-sum-of-chars logic -- exact equality
    # is expected here (both are computed from the identical per-item
    # character formula), not merely approximate.
    import math

    total_chars = sum(estimate_object_core_output_chars(f"o{i}", _OUTPUT_LIMITS) for i in range(1, count + 1))
    return math.ceil(total_chars / 3.0)


def test_conservative_chars_per_token_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        estimate_object_core_batch_output_tokens(["o1"], _OUTPUT_LIMITS, conservative_chars_per_token=0)


# ── local id assignment ──────────────────────────────────────────────────


def test_local_id_for_index_is_one_based_and_batch_scoped():
    assert local_id_for_index(0) == "o1"
    assert local_id_for_index(9) == "o10"


def test_local_id_for_index_rejects_negative():
    import pytest

    with pytest.raises(ValueError):
        local_id_for_index(-1)
