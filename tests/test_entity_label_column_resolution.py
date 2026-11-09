"""
Enterprise Phase 4 — Automatic Business Vocabulary: entity label-column
resolution (data.query_planning_service._label_column_bonus /
_resolve_entity_label_column).

Covers the second half of the a real production database "program" gap: even once a term
naming a grounded entity (e.g. "program" -> dbo.EDU_Track) is resolved at the
TABLE level, none of that table's own dimension-eligible columns lexically
match the term itself (TrackName != "program") — the ordinary scored/
_AUTO_SELECT_MIN_CONFIDENCE-gated column loop can never clear its own bar.
_resolve_entity_label_column answers the narrower, structural question
"which of this table's own columns is its display label" directly, the same
architecture _resolve_contract_status_column already uses for the "status"
role.

Run:
    python -m pytest tests/test_entity_label_column_resolution.py -v
"""
from __future__ import annotations

from data.query_planning_service import (
    _label_column_bonus,
    _resolve_entity_label_column,
    _resolve_term,
)


def _col(column_name, *, is_dimension=True, business_label=None):
    return {
        "column_name": column_name,
        "dictionary": {
            "business_label": business_label,
            "is_dimension": is_dimension,
            "is_metric": False,
            "is_date": False,
        },
        "profiling": {},
        "schema": {},
    }


# ---------------------------------------------------------------------------
# _label_column_bonus — pure
# ---------------------------------------------------------------------------

def test_label_column_full_bonus_when_name_embeds_table_core_token():
    col = _col("TrackName", business_label="Path Name")
    result = _label_column_bonus(col, "dbo.EDU_Track")
    assert result["bonus"] > 0
    assert "Primary name/label column" in result["reason"]


def test_label_column_weak_bonus_when_name_token_present_but_unrelated_to_table():
    # Same table, a DIFFERENT "*Name" column for an unrelated sub-concept
    # (dbo.EDU_Track really has both TrackName and CertificateName) — must
    # not win as decisively as the table's own eponymous label column.
    path_name = _label_column_bonus(_col("TrackName"), "dbo.EDU_Track")
    certificate_name = _label_column_bonus(_col("CertificateName"), "dbo.EDU_Track")
    assert certificate_name["bonus"] > 0
    assert certificate_name["bonus"] < path_name["bonus"]


def test_label_column_zero_bonus_for_non_label_column():
    result = _label_column_bonus(_col("TrackDesc"), "dbo.EDU_Track")
    assert result["bonus"] == 0.0
    assert result["reason"] is None


def test_label_column_via_business_label_token_not_just_column_name():
    col = _col("CertName", business_label="Certificate Name")
    result = _label_column_bonus(col, "dbo.EDU_Track")
    assert result["bonus"] > 0


# ---------------------------------------------------------------------------
# _resolve_entity_label_column — one table's own column pool
# ---------------------------------------------------------------------------

def test_resolves_the_sole_full_match_label_column():
    ctx = {"columns": [
        _col("TrackID", is_dimension=False),
        _col("TrackName"),
        _col("TrackDesc"),
        _col("CertificateName"),
    ]}
    result = _resolve_entity_label_column("dbo.EDU_Track", ctx)
    assert result is not None
    assert result["column_name"] == "TrackName"
    assert result["table_fqn"] == "dbo.EDU_Track"
    assert result["score"] == 1.0


def test_returns_none_when_no_dimension_columns():
    ctx = {"columns": [_col("TrackID", is_dimension=False)]}
    assert _resolve_entity_label_column("dbo.EDU_Track", ctx) is None


def test_returns_none_when_no_column_embeds_table_core_token():
    # Only a weak, unrelated "*Name" column exists — genuine ambiguity
    # (nothing clearly IS the table's own label) must refuse, not guess.
    ctx = {"columns": [_col("TrackDesc"), _col("CertificateName")]}
    assert _resolve_entity_label_column("dbo.EDU_Track", ctx) is None


def test_returns_none_when_two_columns_both_embed_table_core_token():
    ctx = {"columns": [_col("TrackName"), _col("TrackTitle", business_label="Track Title")]}
    assert _resolve_entity_label_column("dbo.EDU_Track", ctx) is None


def test_empty_ctx_returns_none():
    assert _resolve_entity_label_column("dbo.EDU_Track", {}) is None


# ---------------------------------------------------------------------------
# _resolve_term integration — the fallback fires only when the ordinary
# lexical/scored loop found nothing confident, and only for a term that
# names the grounded entity itself (contract role == "canonical").
# ---------------------------------------------------------------------------

def _grounding_for(entity_name, table_fqn):
    return {
        "grounded": {
            entity_name: {"canonical_table_fqn": table_fqn, "contract": {}},
        },
    }


def test_resolve_term_dimension_falls_back_to_entity_label_column():
    ctx = {"columns": [
        _col("TrackID", is_dimension=False),
        _col("TrackName"),
        _col("TrackDesc"),
    ]}
    table_contexts = {"dbo.EDU_Track": ctx}
    grounding = _grounding_for("Program", "dbo.EDU_Track")

    result = _resolve_term("program", table_contexts, "dimension", None, None, grounding=grounding)
    assert result["selected"] is not None
    assert result["selected"]["column_name"] == "TrackName"
    assert result["warnings"] == []


def test_resolve_term_does_not_use_label_fallback_for_measure_kind():
    # The label-column fallback is dimension-only — a measure resolution
    # with nothing lexically confident must still report ambiguous/missing,
    # never silently substitute a "label" column as a metric.
    ctx = {"columns": [
        _col("TrackID", is_dimension=False),
        _col("TrackName"),
    ]}
    table_contexts = {"dbo.EDU_Track": ctx}
    grounding = _grounding_for("Program", "dbo.EDU_Track")

    result = _resolve_term("program", table_contexts, "measure", None, None, grounding=grounding)
    assert result["selected"] is None


def test_resolve_term_real_lexical_match_still_wins_over_label_fallback():
    # A column that genuinely, lexically matches the term must win outright
    # via the ordinary scored loop — the label fallback must never override
    # real evidence, only fill a genuine gap.
    lexical_col = _col("ProgramName", business_label="Program Name")
    ctx = {"columns": [
        _col("TrackID", is_dimension=False),
        lexical_col,
        _col("TrackDesc"),
    ]}
    table_contexts = {"dbo.EDU_Track": ctx}
    grounding = _grounding_for("Program", "dbo.EDU_Track")

    result = _resolve_term("program", table_contexts, "dimension", None, None, grounding=grounding)
    assert result["selected"] is not None
    assert result["selected"]["column_name"] == "ProgramName"
