"""
Focused unit tests — SQL Architecture Correction, Phase 6, Step 6G, Part 3:
relevance-first anchor selection (core.sqlservice.schema_linker._select_
anchors / _structural_component_score).

Pure/offline: no database connection, no OpenAI call, no live SQL Server,
no network of any kind. Exercises the private selection function directly
with hand-built _ScoredCandidate fixtures -- the precise, minimal way to
prove the exact greedy-ordering behavior Part 3 requires without needing a
full question/catalog pipeline to reproduce a scarcity of anchor slots.

Background: Phase 6, Step 6F's own offline evaluation traced a real
production regression (required-object coverage most -> fewer -> fewest on
the the reference database golden cases, purely from enabling real view semantics) to
_select_anchors' original Phase 1 loop, which greedily preferred whichever
admitted candidate covered the MOST currently-uncovered required concepts,
breaking ties by combined_score only secondarily. A business view whose
validated model-inferred business_domain/alias/status/operation evidence
happens to nominally touch several distinct requirements at once (exactly
what a denormalizing view legitimately does) could out-rank a single,
strongly, directly relevant object for a requirement it actually served
best. These tests pin the corrected ordering: combined_score (direct
relevance/match strength) first, structural-vs-model tiebreak second,
breadth only third.

Run:
    venv/Scripts/pytest tests/test_sqlservice_schema_linker_anchor_selection.py -v
"""

from __future__ import annotations

import random

from core.sqlservice.schema_linker import (
    ADMISSION_REASON_CONCEPT_MATCH,
    ADMISSION_REASON_MODEL_MATCH,
    ANCHOR_WEIGHT_MODEL_SEMANTIC,
    _ScoredCandidate,
    _select_anchors,
    _structural_component_score,
)


def _cand(
    object_fqn: str,
    supporting_concept_refs: tuple[str, ...],
    combined_score: float,
    *,
    model_semantic_score: float = 0.0,
    admission_reasons: tuple[str, ...] = (ADMISSION_REASON_CONCEPT_MATCH,),
    object_type: str = "TABLE",
) -> _ScoredCandidate:
    return _ScoredCandidate(
        object_fqn=object_fqn, object_type=object_type, retrieval_tier=1,
        supporting_concept_refs=supporting_concept_refs, supporting_columns=(),
        semantic_score=None, lexical_score=None, catalog_score=combined_score,
        example_score=0.0, canonical_score=0.0, model_semantic_score=model_semantic_score,
        combined_score=combined_score, evidence=(), is_admitted=True, admission_reasons=admission_reasons,
    )


# ── One strong match beats several weak, broad ones ───────────────────────


def test_one_strong_narrow_match_beats_a_weak_broad_match_covering_more_concepts():
    """A weak candidate nominally covering THREE required concepts must not
    be selected ahead of a single strong candidate that covers only ONE --
    the exact REF2-style regression: a business view's broad, low-
    confidence model-inferred coverage must never out-prioritize a single
    strongly, directly relevant object."""
    required = frozenset({"ENTITY:student", "MEASURE:amount", "DIMENSION:status"})
    weak_broad_view = _cand(
        "dbo.vw_Broad", ("ENTITY:student", "MEASURE:amount", "DIMENSION:status"), combined_score=0.20,
        model_semantic_score=0.40, admission_reasons=(ADMISSION_REASON_MODEL_MATCH,),
    )
    strong_narrow = _cand("dbo.Students", ("ENTITY:student",), combined_score=0.90)

    selected, coverage_incomplete = _select_anchors([weak_broad_view, strong_narrow], required, max_anchors=10)
    selected_fqns = [c.object_fqn for c in selected]

    assert selected_fqns[0] == "dbo.Students", (
        "the strong, narrowly-relevant candidate must be picked FIRST, "
        f"regardless of breadth: got {selected_fqns!r}"
    )
    # The weak broad view is still eligible to be picked up afterward for
    # whatever it uniquely covers (MEASURE:amount, DIMENSION:status) --
    # Part 3 never says a broad candidate can NEVER be selected, only that
    # breadth alone cannot let it jump the queue ahead of real strength.
    assert set(selected_fqns) == {"dbo.Students", "dbo.vw_Broad"}
    assert not coverage_incomplete


def test_breadth_still_matters_as_a_tiebreak_among_equal_strength_candidates():
    """When two candidates are equally strong, the one covering MORE of
    the currently-uncovered requirements is still preferred -- breadth is
    demoted to a tiebreaker, never eliminated."""
    required = frozenset({"ENTITY:student", "MEASURE:amount"})
    broad_equal_strength = _cand("dbo.Broad", ("ENTITY:student", "MEASURE:amount"), combined_score=0.70)
    narrow_equal_strength = _cand("dbo.Narrow", ("ENTITY:student",), combined_score=0.70)

    selected, _ = _select_anchors([broad_equal_strength, narrow_equal_strength], required, max_anchors=10)
    assert [c.object_fqn for c in selected] == ["dbo.Broad"]


# ── Structural evidence outranks model evidence at equal combined_score ──


def test_structural_evidence_breaks_ties_ahead_of_model_evidence():
    """Two candidates reaching the SAME combined_score -- one earning it
    mostly through validated-but-advisory model inference, the other
    through real catalog/lexical/semantic/example evidence -- must never
    be treated as equally trustworthy; the structurally-evidenced one is
    preferred."""
    required = frozenset({"ENTITY:student"})
    model_heavy = _cand(
        "dbo.ModelHeavy", ("ENTITY:student",), combined_score=0.60,
        model_semantic_score=0.60 / ANCHOR_WEIGHT_MODEL_SEMANTIC if ANCHOR_WEIGHT_MODEL_SEMANTIC else 0.0,
    )
    # Cap model_semantic_score at 1.0 (a real MODEL_CANDIDATE_CONFIDENCE_CAP
    # of 0.50 in production, but 1.0 is the dataclass's own valid range and
    # produces the maximum possible structural-vs-model score GAP for this
    # test's purposes).
    model_heavy = _cand("dbo.ModelHeavy", ("ENTITY:student",), combined_score=0.60, model_semantic_score=1.0)
    structural = _cand("dbo.Structural", ("ENTITY:student",), combined_score=0.60, model_semantic_score=0.0)

    assert _structural_component_score(structural) > _structural_component_score(model_heavy)

    selected, _ = _select_anchors([model_heavy, structural], required, max_anchors=10)
    assert selected[0].object_fqn == "dbo.Structural"


# ── Deterministic ordering / renaming invariance ──────────────────────────


def test_selection_is_deterministic_regardless_of_input_order():
    required = frozenset({"ENTITY:a", "ENTITY:b", "ENTITY:c"})
    candidates = [
        _cand("dbo.A", ("ENTITY:a",), combined_score=0.81),
        _cand("dbo.B", ("ENTITY:b",), combined_score=0.55),
        _cand("dbo.C", ("ENTITY:c",), combined_score=0.55),
        _cand("dbo.Broad", ("ENTITY:a", "ENTITY:b", "ENTITY:c"), combined_score=0.30),
    ]
    baseline_fqns = [c.object_fqn for c in _select_anchors(list(candidates), required, max_anchors=10)[0]]
    for _ in range(20):
        shuffled = list(candidates)
        random.shuffle(shuffled)
        result_fqns = [c.object_fqn for c in _select_anchors(shuffled, required, max_anchors=10)[0]]
        assert result_fqns == baseline_fqns


def test_selection_invariant_under_object_renaming():
    """The algorithm must be a pure function of scores/concept-support --
    never of the object's own name/spelling (no the reference database or any other
    customer-specific behavior baked in)."""
    required_a = frozenset({"ENTITY:x"})
    result_a, _ = _select_anchors(
        [_cand("dbo.Alpha123", ("ENTITY:x",), combined_score=0.71), _cand("dbo.Zeta999", ("ENTITY:x",), combined_score=0.40)],
        required_a, max_anchors=10,
    )
    required_b = frozenset({"ENTITY:x"})
    result_b, _ = _select_anchors(
        [_cand("zzz.CompletelyDifferentName", ("ENTITY:x",), combined_score=0.71), _cand("aaa.AnotherName", ("ENTITY:x",), combined_score=0.40)],
        required_b, max_anchors=10,
    )
    # Same RELATIVE outcome (the higher-scoring one wins) regardless of
    # what either object happens to be named or how their names sort
    # alphabetically (note "zzz..." would lose an alphabetical tiebreak
    # but must still win here on combined_score alone).
    assert result_a[0].combined_score == 0.71
    assert result_b[0].combined_score == 0.71
    assert result_b[0].object_fqn == "zzz.CompletelyDifferentName"


def test_tied_candidates_break_ties_by_object_fqn_ascending():
    required = frozenset({"ENTITY:x"})
    candidates = [
        _cand("dbo.Zulu", ("ENTITY:x",), combined_score=0.50),
        _cand("dbo.Alpha", ("ENTITY:x",), combined_score=0.50),
    ]
    selected, _ = _select_anchors(candidates, required, max_anchors=10)
    assert selected[0].object_fqn == "dbo.Alpha"


# ── Missing mandatory requirements cannot be hidden ────────────────────────


def test_uncoverable_requirement_reported_incomplete_not_silently_dropped():
    required = frozenset({"ENTITY:student", "ENTITY:nonexistent"})
    candidates = [_cand("dbo.Students", ("ENTITY:student",), combined_score=0.90)]
    selected, coverage_incomplete = _select_anchors(candidates, required, max_anchors=10)
    assert coverage_incomplete is True
    assert [c.object_fqn for c in selected] == ["dbo.Students"]
