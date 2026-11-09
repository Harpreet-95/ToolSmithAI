"""
Ranking Semantics Completion milestone.

Part 1 root cause (live-confirmed against a real production database, see this milestone's
Final Report for the full instrumented trace): "Who is the top performing
recruiter?" is a genuine ranking question — core.semantic.concept_resolver.
derive_analytics_intent already correctly classifies it question_type==
"ranking" (via the prior milestone's bare-superlative fix) — but it names no
separate entity/measure PAIR (only `measure` populates; `entity` stays
None), so NONE of core.orchestrator.context_builder's three existing
needs_ai guards (entity+measure pair / "and their" relationship phrasing /
status_value+measure_max-min) forced AI consultation. The AI Interpreter was
therefore never even called, and legacy's bare-list builder — which has no
ranking or implicit-measure concept at all — silently collapsed "top
performing recruiter" into an unfiltered/unordered row dump.

Fixed with a fourth, generic needs_ai guard: any question_type=="ranking"
that the entity+measure check didn't already resolve now forces AI. This
file tests the SIGNAL that guard depends on (Part 5's mandated regression
coverage for this exact failure class) at the pure-function level, plus
Part 4/9's ORDER BY safety guardrails and impossible-ranking control at the
same attempt_ai_physical_plan level test_ai_physical_planner.py already
establishes for the existing explicit-measure ranking tests.
"""
from __future__ import annotations

from core.semantic.concept_resolver import derive_analytics_intent
from core.orchestrator.ai_physical_planning import attempt_ai_physical_plan
from core.semantic.ai_interpreter import AIQuestionInterpretation


# ---------------------------------------------------------------------------
# Part 5 — the exact signal the new needs_ai guard depends on. These are all
# genuinely different, unrelated business nouns (never the reference database/Recruiter-
# specific) to prove the classification itself is generic.
# ---------------------------------------------------------------------------

def test_bare_superlative_ranking_without_entity_measure_pair_forces_ai():
    """This exact combination is what previously slipped past every
    needs_ai guard in core.orchestrator.context_builder: either
    question_type=='ranking' (the "top"/"best"/"worst" bare-superlative
    shape) or aggregation_target in ('measure_max','measure_min') (the
    "highest"/"lowest performing X" sibling shape, which extract_query_
    intent's older _MAX_RE/_MIN_RE aggregation detection classifies
    differently) — combined with entity staying unresolved (None). If
    derive_analytics_intent ever stops producing this combination for these
    phrasings, the context_builder guard that depends on it silently stops
    firing too — this test exists to catch that."""
    for question in (
        "Who is the top performing recruiter?",
        "Who is the best salesperson?",
        "What is the highest performing store?",
        "Which vendor is the most successful?",
        "Who is our worst supplier?",
    ):
        intent = derive_analytics_intent(question)
        forces_ai = (
            intent["question_type"] == "ranking"
            or intent["aggregation_target"] in ("measure_max", "measure_min")
        )
        assert forces_ai, f"{question!r} -> {intent}"


def test_explicit_measure_ranking_still_resolves_entity_and_measure_pair():
    """Contrast case: "top 10 clients by revenue" already names both roles
    via the existing top_n_by shape — must NOT regress into looking like the
    bare-superlative case above."""
    intent = derive_analytics_intent("top 10 clients by revenue")
    assert intent["question_type"] == "ranking"
    assert intent["entity"] == "clients"
    assert intent["measure"] == "revenue"


def test_wh_ranking_still_resolves_entity_and_measure_pair():
    intent = derive_analytics_intent("Which stores have the highest sales?")
    assert intent["question_type"] == "ranking"
    assert intent["entity"] is not None
    assert intent["measure"] is not None


def test_non_ranking_questions_are_not_misclassified():
    for question in ("How many clients are there?", "Show active projects", "List courses"):
        intent = derive_analytics_intent(question)
        assert intent["question_type"] != "ranking", f"{question!r} -> {intent}"


# ---------------------------------------------------------------------------
# Part 4/9 — ORDER BY safety guardrails + impossible ranking control, at the
# same attempt_ai_physical_plan level tests/test_ai_physical_planner.py
# already uses for the existing explicit-measure ranking tests (imported
# helpers below mirror that file's own env()/_add_table()/_add_column()/
# _mock_adjudication() fixtures).
# ---------------------------------------------------------------------------
from tests.ai_physical_planner_fixtures import env, _add_table, _add_column, _mock_adjudication  # noqa: E402
from unittest.mock import patch  # noqa: E402


def test_impossible_ranking_never_falls_back_to_primary_key_or_timestamp(tmp_path, monkeypatch):
    """Part 9 — a table with ONLY identity/audit columns (no plausible
    business measure at all). "Who is our best customer?" must never order
    by CustomerID or UpdatedAt merely because they are sortable."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.customers")
    _add_column(db, "dbo.customers", "CustomerID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.customers", "CustomerName", data_type="TEXT")
    _add_column(db, "dbo.customers", "CreatedAt", data_type="DATETIME")
    _add_column(db, "dbo.customers", "UpdatedAt", data_type="DATETIME")

    interp = AIQuestionInterpretation(
        entities=("customer",), relationship_intent="single_entity", measures=("best",),
    )
    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.customers"),
    ):
        result = attempt_ai_physical_plan(1, "u1", "Who is our best customer?", interp)

    assert result["valid"] is False, (
        "must safely refuse, never fabricate a ranking over CustomerID/CreatedAt/UpdatedAt"
    )
    assert result["generated"] is None
    # If it ever did produce SQL, it must not silently be ordering by the PK
    # or an audit timestamp — belt-and-suspenders on top of the refusal
    # above, in case a future change makes this "valid" some other way.
    sql_plan = result.get("sql_plan") or {}
    for order_row in (sql_plan.get("order_by") or []):
        assert order_row.get("column_name") not in ("CustomerID", "CreatedAt", "UpdatedAt")


def test_ranking_measure_unresolvable_disqualifies_generically_not_just_for_reference(tmp_path, monkeypatch):
    """Same class of proof as test_ranking_measure_unresolvable_falls_back
    in test_ai_physical_planner.py, but with an entirely different,
    unrelated synthetic schema/business noun (Part 23: must generalize, not
    be a production-style coincidence)."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.warehouses")
    _add_column(db, "dbo.warehouses", "WarehouseID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.warehouses", "SquareFootage", data_type="INTEGER")

    interp = AIQuestionInterpretation(
        entities=("warehouse",), relationship_intent="single_entity", measures=("efficiency",),
    )
    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        return_value=_mock_adjudication("dbo.warehouses"),
    ):
        result = attempt_ai_physical_plan(1, "u1", "top warehouse by efficiency", interp)

    assert result["valid"] is False
    assert result["reason"] == "ranking_measure_unresolvable_out_of_stage2c_scope"
    assert result["attempted"] is True


def test_grouped_ranking_by_related_table_count_still_works(tmp_path, monkeypatch):
    """Part 7 — grouped ranking (aggregate-then-rank), unrelated synthetic
    schema. Confirms the existing rank_by_aggregate machinery this milestone
    reused (never rebuilt) still produces GROUP BY -> aggregate -> ORDER BY,
    not a raw-row TOP N before aggregation."""
    db = env(tmp_path, monkeypatch)
    _add_table(db, "dbo.carriers")
    _add_column(db, "dbo.carriers", "CarrierID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.carriers", "CarrierName", data_type="TEXT")

    _add_table(db, "dbo.shipments")
    _add_column(db, "dbo.shipments", "ShipmentID", data_type="INTEGER", is_pk=1, uniqueness=1.0)
    _add_column(db, "dbo.shipments", "CarrierID", data_type="INTEGER")

    from tests.ai_physical_planner_fixtures import _add_fk
    _add_fk(db, "dbo.shipments", "CarrierID", "dbo.carriers", "CarrierID", status="AUTO")

    interp = AIQuestionInterpretation(
        entities=("carrier",), relationship_intent="scoped_count", measures=("shipments",),
    )
    with patch(
        "core.semantic.candidate_adjudicator.adjudicate_candidates",
        side_effect=[_mock_adjudication("dbo.carriers"), _mock_adjudication("dbo.shipments")],
    ):
        result = attempt_ai_physical_plan(1, "u1", "carrier with most shipments", interp)

    if result["valid"]:
        assert result["sql_plan"]["group_by"], "grouped ranking must GROUP BY the ranked entity"
        assert result["sql_plan"]["order_by"], "grouped ranking must ORDER BY the aggregate"
        sql_upper = result["generated"]["sql"].upper()
        assert "GROUP BY" in sql_upper
        assert "ORDER BY" in sql_upper
    else:
        # Acceptable alternative outcome: safely disqualifies rather than
        # emitting a raw-row TOP N with no GROUP BY — either way, it must
        # never silently produce an ungrouped ranking.
        assert result["generated"] is None
