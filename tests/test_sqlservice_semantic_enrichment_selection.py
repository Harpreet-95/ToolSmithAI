"""
Focused unit tests — SQL Architecture Correction, Phase 6, Step 4C1:
deterministic column selection and output-budget estimation
(core/sqlservice/semantic_enrichment_selection.py).

Pure/offline: no database connection, no real OpenAI call, no live SQL
Server, no network of any kind.

Run:
    venv/Scripts/pytest tests/test_sqlservice_semantic_enrichment_selection.py -v
"""

from __future__ import annotations

import pytest

from core.sqlservice.semantic_enrichment_selection import (
    REASON_AMBIGUOUS_ROLE,
    REASON_BUSINESS_KEY_CANDIDATE,
    REASON_CLASSIFICATION_CONFLICT,
    REASON_DICTIONARY_FLAGGED_INCOMPLETE,
    REASON_NAME_TYPE_MISMATCH,
    REASON_OPAQUE_NAME,
    REASON_RELATIONSHIP_ROLE_UNCLEAR,
    ColumnSelectionLimits,
    OutputEstimateLimits,
    SelectedColumn,
    estimate_batch_output_tokens,
    estimate_column_output_chars,
    estimate_object_output_chars,
    reduce_selected_columns,
    select_columns_for_enrichment,
)
from core.sqlservice.semantic_enrichment_service import ColumnSemanticInput

_DEFAULT_LIMITS = ColumnSelectionLimits(
    max_selected_columns_per_object=12,
    max_opaque_candidates_per_object=6,
    max_relationship_context_columns_per_object=6,
)

_DEFAULT_METADATA = {
    "date_columns": (), "measure_columns": (), "status_columns": (),
    "confirmed_relationship_edges": (),
}


def _col(name, sql_type="OTHER", *, pk=False, fk=False):
    return ColumnSemanticInput(column_name=name, normalized_sql_type=sql_type, is_primary_key=pk, is_foreign_key=fk)


def _select(columns, *, metadata=None, dictionary_business_name=None, dictionary_description=None, limits=None):
    return select_columns_for_enrichment(
        columns,
        metadata=dict(_DEFAULT_METADATA, **(metadata or {})),
        dictionary_business_name=dictionary_business_name,
        dictionary_description=dictionary_description,
        limits=limits or _DEFAULT_LIMITS,
    )


def _names(selected):
    return {s.column_name for s in selected}


# ── Structurally obvious columns are never selected ───────────────────────


def test_real_primary_key_with_clear_name_is_not_selected():
    columns = [_col("EnrollmentID", "INTEGER", pk=True)]
    selected = _select(columns)
    assert _names(selected) == set()


def test_ordinary_descriptive_text_column_is_not_selected():
    columns = [_col("Description", "TEXT")]
    selected = _select(columns)
    assert _names(selected) == set()


def test_fk_column_whose_name_matches_its_target_is_not_selected():
    columns = [_col("StudentID", "INTEGER", fk=True)]
    metadata = {"confirmed_relationship_edges": ({"direction": "outbound", "column": "StudentID", "other": "dbo.Student"},)}
    selected = _select(columns, metadata=metadata)
    assert _names(selected) == set()


# ── Each condition selects a column, with the right reason ────────────────


def test_opaque_name_selected():
    columns = [_col("Txn", "TEXT")]
    selected = _select(columns)
    assert _names(selected) == {"Txn"}
    assert selected[0].reasons == (REASON_OPAQUE_NAME,)


def test_name_type_mismatch_selected_for_date_named_text_column():
    columns = [_col("CreatedDate", "TEXT")]
    selected = _select(columns)
    assert _names(selected) == {"CreatedDate"}
    assert REASON_NAME_TYPE_MISMATCH in selected[0].reasons


def test_name_type_mismatch_selected_for_measure_named_text_column():
    columns = [_col("TotalAmount", "TEXT")]
    selected = _select(columns)
    assert _names(selected) == {"TotalAmount"}
    assert REASON_NAME_TYPE_MISMATCH in selected[0].reasons


def test_classification_conflict_selected_when_multiple_hints_match():
    # "StatusDate" hints both STATUS ("status") and DATE ("date").
    columns = [_col("StatusDate", "DATETIME")]
    selected = _select(columns)
    assert _names(selected) == {"StatusDate"}
    assert REASON_CLASSIFICATION_CONFLICT in selected[0].reasons


def test_ambiguous_role_selected_when_type_unknown():
    columns = [_col("EnrollDate", "OTHER")]
    metadata = {"date_columns": ("enrolldate",)}
    selected = _select(columns, metadata=metadata)
    assert _names(selected) == {"EnrollDate"}
    assert REASON_AMBIGUOUS_ROLE in selected[0].reasons


def test_business_key_candidate_selected_for_non_pk_fk_identifier_name():
    columns = [_col("ExternalCode", "TEXT")]
    selected = _select(columns)
    assert _names(selected) == {"ExternalCode"}
    assert REASON_BUSINESS_KEY_CANDIDATE in selected[0].reasons


def test_business_key_candidate_never_fires_for_a_real_pk_or_fk():
    columns = [_col("RecordCode", "TEXT", pk=True)]
    selected = _select(columns)
    assert REASON_BUSINESS_KEY_CANDIDATE not in {r for s in selected for r in s.reasons}


def test_relationship_role_unclear_selected_for_generic_fk_name():
    columns = [_col("RefID", "INTEGER", fk=True)]
    metadata = {"confirmed_relationship_edges": ({"direction": "outbound", "column": "RefID", "other": "dbo.Instructor"},)}
    selected = _select(columns, metadata=metadata)
    assert _names(selected) == {"RefID"}
    assert REASON_RELATIONSHIP_ROLE_UNCLEAR in selected[0].reasons


def test_dictionary_flagged_incomplete_selected_for_key_column():
    columns = [_col("EnrollmentID", "INTEGER", pk=True)]
    selected = _select(columns, dictionary_business_name="Enrollment", dictionary_description=None)
    assert _names(selected) == {"EnrollmentID"}
    assert REASON_DICTIONARY_FLAGGED_INCOMPLETE in selected[0].reasons


def test_dictionary_flagged_incomplete_never_fires_when_description_present():
    columns = [_col("EnrollmentID", "INTEGER", pk=True)]
    selected = _select(columns, dictionary_business_name="Enrollment", dictionary_description="An enrollment record")
    assert _names(selected) == set()


# ── Determinism ────────────────────────────────────────────────────────────


def test_selection_is_deterministic_across_repeated_calls():
    columns = [_col("Txn", "TEXT"), _col("CreatedDate", "TEXT"), _col("StudentID", "INTEGER")]
    first = _select(columns)
    second = _select(columns)
    assert first == second


def test_selection_reasons_are_reported_in_stable_priority_order():
    # "CreatedDate" is BOTH a NAME_TYPE_MISMATCH (date hint vs TEXT type)
    # and, if it were also flagged ambiguous, would report the higher-
    # priority reason first -- verify NAME_TYPE_MISMATCH sorts before any
    # lower-priority reason a column might also match.
    columns = [_col("CreatedDate", "TEXT")]
    selected = _select(columns)
    assert selected[0].reasons[0] == REASON_NAME_TYPE_MISMATCH


# ── Bounded, priority-ordered caps ─────────────────────────────────────────


def test_max_selected_columns_per_object_cap_keeps_highest_priority_first():
    # 3 opaque-ONLY columns (lowest tier) + 1 name/type-mismatch column
    # (highest tier); cap of 2 must keep the mismatch column and exactly
    # one opaque column, never two opaque columns while dropping the
    # mismatch one.
    columns = [_col("Txn", "TEXT"), _col("F1", "TEXT"), _col("Cd", "TEXT"), _col("CreatedDate", "TEXT")]
    limits = ColumnSelectionLimits(
        max_selected_columns_per_object=2, max_opaque_candidates_per_object=6,
        max_relationship_context_columns_per_object=6,
    )
    selected = _select(columns, limits=limits)
    assert len(selected) == 2
    assert "CreatedDate" in _names(selected)


def test_max_opaque_candidates_per_object_cap():
    columns = [_col("Txn", "TEXT"), _col("F1", "TEXT"), _col("Cd", "TEXT")]
    limits = ColumnSelectionLimits(
        max_selected_columns_per_object=12, max_opaque_candidates_per_object=1,
        max_relationship_context_columns_per_object=6,
    )
    selected = _select(columns, limits=limits)
    assert len(selected) == 1


def test_max_relationship_context_columns_per_object_cap():
    # Both FK names are non-opaque and unrelated to their targets, so each
    # matches RELATIONSHIP_ROLE_UNCLEAR only (not also OPAQUE_NAME) --
    # cleanly isolating the relationship-context sub-cap.
    columns = [_col("OwnerID", "INTEGER", fk=True), _col("ParentID", "INTEGER", fk=True)]
    metadata = {
        "confirmed_relationship_edges": (
            {"direction": "outbound", "column": "OwnerID", "other": "dbo.Instructor"},
            {"direction": "outbound", "column": "ParentID", "other": "dbo.Category"},
        ),
    }
    limits = ColumnSelectionLimits(
        max_selected_columns_per_object=12, max_opaque_candidates_per_object=6,
        max_relationship_context_columns_per_object=1,
    )
    selected = _select(columns, metadata=metadata, limits=limits)
    assert len(selected) == 1


def test_reduce_selected_columns_drops_lowest_priority_first():
    selected = (
        SelectedColumn(column_name="A", reasons=(REASON_OPAQUE_NAME,)),
        SelectedColumn(column_name="B", reasons=(REASON_NAME_TYPE_MISMATCH,)),
        SelectedColumn(column_name="C", reasons=(REASON_BUSINESS_KEY_CANDIDATE,)),
    )
    reduced = reduce_selected_columns(selected, 1)
    assert [s.column_name for s in reduced] == ["B"]


def test_reduce_selected_columns_to_zero_returns_empty():
    selected = (SelectedColumn(column_name="A", reasons=(REASON_OPAQUE_NAME,)),)
    assert reduce_selected_columns(selected, 0) == ()


def test_reduce_selected_columns_noop_when_already_within_target():
    selected = (SelectedColumn(column_name="A", reasons=(REASON_OPAQUE_NAME,)),)
    assert reduce_selected_columns(selected, 5) == selected


# ── Output-budget estimation ────────────────────────────────────────────────

_ESTIMATE_LIMITS = OutputEstimateLimits(
    max_business_name_characters=150, max_description_characters=500,
    max_concepts_per_object=10, max_concept_characters=60,
    max_aliases_per_object=15, max_alias_characters=60,
    max_operations_per_object=8, max_business_label_characters=150,
)


def test_estimate_object_output_chars_scales_with_configured_maximums():
    small = estimate_object_output_chars("dbo.Stu", _ESTIMATE_LIMITS)
    bigger_limits = OutputEstimateLimits(
        max_business_name_characters=300, max_description_characters=500,
        max_concepts_per_object=10, max_concept_characters=60,
        max_aliases_per_object=15, max_alias_characters=60,
        max_operations_per_object=8, max_business_label_characters=150,
    )
    bigger = estimate_object_output_chars("dbo.Stu", bigger_limits)
    assert bigger - small == 150  # exactly the business-name delta, nothing else changed


def test_estimate_object_output_chars_includes_actual_object_fqn_length():
    short = estimate_object_output_chars("dbo.A", _ESTIMATE_LIMITS)
    long = estimate_object_output_chars("dbo.SomeMuchLongerObjectName", _ESTIMATE_LIMITS)
    assert long - short == len("dbo.SomeMuchLongerObjectName") - len("dbo.A")


def test_estimate_column_output_chars_scales_with_business_label_maximum():
    small = estimate_column_output_chars("Col", _ESTIMATE_LIMITS)
    bigger_limits = OutputEstimateLimits(
        max_business_name_characters=150, max_description_characters=500,
        max_concepts_per_object=10, max_concept_characters=60,
        max_aliases_per_object=15, max_alias_characters=60,
        max_operations_per_object=8, max_business_label_characters=300,
    )
    bigger = estimate_column_output_chars("Col", bigger_limits)
    assert bigger - small == 150


def test_estimate_batch_output_tokens_uses_conservative_ratio_conversion():
    batch = [("dbo.Stu", 0, ())]
    low_ratio_tokens = estimate_batch_output_tokens(batch, _ESTIMATE_LIMITS, conservative_chars_per_token=2.0)
    high_ratio_tokens = estimate_batch_output_tokens(batch, _ESTIMATE_LIMITS, conservative_chars_per_token=4.0)
    # A LOWER chars-per-token ratio must produce a HIGHER (more
    # conservative) token estimate for the identical character budget.
    assert low_ratio_tokens > high_ratio_tokens


def test_estimate_batch_output_tokens_grows_with_selected_column_count():
    no_columns = estimate_batch_output_tokens([("dbo.Stu", 0, ())], _ESTIMATE_LIMITS, conservative_chars_per_token=3.0)
    with_columns = estimate_batch_output_tokens(
        [("dbo.Stu", 2, ("ColA", "ColB"))], _ESTIMATE_LIMITS, conservative_chars_per_token=3.0,
    )
    assert with_columns > no_columns


def test_estimate_batch_output_tokens_rejects_non_positive_ratio():
    with pytest.raises(ValueError):
        estimate_batch_output_tokens([("dbo.Stu", 0, ())], _ESTIMATE_LIMITS, conservative_chars_per_token=0)
