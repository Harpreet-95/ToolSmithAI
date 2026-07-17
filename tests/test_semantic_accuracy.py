"""
Tests for Phase 6.4 — Enterprise Semantic Accuracy.

Pure unit tests for core.semantic.concept_resolver.extract_terms() and the
_LATEST_RE "recent"/"current" extension — no DB, no fixtures, matching this
module's own "deterministic regex/stopword only, no AI/LLM" contract.

Root cause (see docs/ENTERPRISE_DELIVERY_PROGRAM.md, Milestone M-22): a bare
grammatical/modifier word surviving into extract_terms()'s output becomes a
candidate search term in data/query_planning_service.py, and
_collect_candidate_tables() unions candidate tables across every term — so
one junk word (e.g. "percentage", "year", "there") can pull in unrelated
tables that dilute or outrank a real business term's own correct candidates.
Every word added to _STOPWORDS in this milestone already has an independent
handler elsewhere in this file (status_value/order/aggregation/date_range),
so excluding it from term extraction loses no signal.

Run from the project root:
    venv/Scripts/pytest tests/test_semantic_accuracy.py -v
"""
from datetime import date

from core.semantic.concept_resolver import extract_terms, extract_query_intent

_TODAY = date(2026, 7, 12)


# ---------------------------------------------------------------------------
# extract_terms() — grammatical filler excluded
# ---------------------------------------------------------------------------

def test_existential_and_pronoun_filler_excluded():
    concepts, measures, dimensions = extract_terms("How many active clients are there?")
    assert "there" not in concepts
    assert "there" not in measures


def test_auxiliary_verbs_excluded():
    concepts, _, _ = extract_terms("How many candidates do we have?")
    for junk in ("do", "we", "have"):
        assert junk not in concepts
    assert "candidates" in concepts


def test_possessive_artifact_excluded():
    # "month's" tokenizes to "month" + "s" — the stray "s" must not survive.
    concepts, _, _ = extract_terms("Compare this month's revenue to last month's")
    assert "s" not in concepts


# ---------------------------------------------------------------------------
# extract_terms() — status words excluded (already independently handled by
# _STATUS_RE / status_value on the raw question, see extract_query_intent)
# ---------------------------------------------------------------------------

def test_status_words_excluded_from_terms_but_still_detected_as_filters():
    concepts, measures, _ = extract_terms("Show active clients")
    assert "active" not in concepts
    assert "active" not in measures
    assert "clients" in concepts
    # The status word is still captured — just via extract_query_intent, not
    # as a literal candidate search term.
    assert extract_query_intent("Show active clients")["status_value"] == "Active"


def test_all_status_words_excluded():
    concepts, _, _ = extract_terms("Open closed cancelled completed inactive candidates")
    for status_word in ("open", "closed", "cancelled", "completed", "inactive"):
        assert status_word not in concepts
    assert "candidates" in concepts


# ---------------------------------------------------------------------------
# extract_terms() — aggregation/ranking operator words excluded (already
# independently handled by _COUNT_RE/_SUM_RE/_AVG_RE/_MIN_RE/_MAX_RE/
# _LATEST_RE/_EARLIEST_RE/_TOP_N_RE/_BOTTOM_N_RE/_DISTINCT_RE)
# ---------------------------------------------------------------------------

def test_aggregation_operator_words_excluded():
    concepts, _, _ = extract_terms("Highest invoice amount")
    assert "highest" not in concepts
    assert "invoice" in concepts
    assert "amount" in concepts
    assert extract_query_intent("Highest invoice amount")["aggregation"] == "MAX"


def test_ranking_words_excluded():
    concepts, _, _ = extract_terms("Latest invoices")
    assert "latest" not in concepts
    assert "invoices" in concepts


def test_distinct_unique_excluded():
    concepts, _, _ = extract_terms("Unique recruiters")
    assert "unique" not in concepts
    assert "recruiters" in concepts


# ---------------------------------------------------------------------------
# extract_terms() — "this" is filler and excluded, but bare calendar words
# (year/month/quarter/week/day) are DELIBERATELY still allowed through: a
# regression (test_composer_sql_routing.py::test_date_range_filter_end_to_end)
# proved "month" is not always redundant with date_range detection — it is
# also the only term that resolves a literal "order_month" DIMENSION column
# when the question has no "this month"/"last month" phrase for date_range
# to match instead. Date-range detection itself is unaffected either way,
# since it always runs on the raw question string, never this term list.
# ---------------------------------------------------------------------------

def test_this_excluded_but_year_kept():
    concepts, _, _ = extract_terms("Total revenue this year")
    assert "this" not in concepts
    assert "year" in concepts
    assert "revenue" in concepts
    assert extract_query_intent("Total revenue this year", today=_TODAY)["date_range"]["label"] == "this_year"


def test_bare_month_still_resolvable_as_a_dimension_term():
    concepts, _, dimensions = extract_terms("Revenue by month")
    assert dimensions == ["month"]


# ---------------------------------------------------------------------------
# Real business terms are never accidentally excluded
# ---------------------------------------------------------------------------

def test_real_business_terms_survive():
    concepts, measures, dimensions = extract_terms("Revenue by department")
    assert "revenue" in concepts
    assert dimensions == ["department"]


def test_multi_domain_question_keeps_all_real_terms():
    concepts, _, _ = extract_terms("Show candidates, their placements, and the client for each placement")
    for real_term in ("candidates", "placements", "client", "placement"):
        assert real_term in concepts


# ---------------------------------------------------------------------------
# _LATEST_RE extension — "recent"/"current" now produce order info
# ---------------------------------------------------------------------------

def test_recent_produces_latest_style_order():
    order = extract_query_intent("Recent placements")["order"]
    assert order is not None
    assert order["direction"] == "DESC"
    assert order["target"] == "date"


def test_current_produces_latest_style_order():
    order = extract_query_intent("Current payroll")["order"]
    assert order is not None
    assert order["direction"] == "DESC"
    assert order["target"] == "date"


def test_latest_and_newest_unaffected():
    # Non-regression: the original M-1 behavior is unchanged.
    order = extract_query_intent("Latest invoices")["order"]
    assert order["direction"] == "DESC"
    assert order["target"] == "date"


# ---------------------------------------------------------------------------
# Sprint 1.4 — "Which <entity> has/have/teach/executed the highest/most/
# largest <measure>?" ranking pattern: entity -> dimension, measure -> measure.
# ---------------------------------------------------------------------------

def test_which_courses_highest_enrollment():
    concepts, measures, dimensions = extract_terms("Which courses have the highest enrollment?")
    assert "courses" in dimensions
    assert "enrollment" in measures
    intent = extract_query_intent("Which courses have the highest enrollment?")
    assert intent["aggregation"] == "MAX"
    assert intent["aggregation_target"] == "measure_max"


def test_which_instructors_most_classes():
    concepts, measures, dimensions = extract_terms("Which instructors teach the most classes?")
    assert "instructors" in dimensions
    assert "classes" in measures
    intent = extract_query_intent("Which instructors teach the most classes?")
    assert intent["aggregation"] == "MAX", "'most' must be recognized consistently with highest/largest/max"
    assert intent["aggregation_target"] == "measure_max"


def test_which_ai_agents_most_workflows():
    concepts, measures, dimensions = extract_terms("Which AI agents executed the most workflows?")
    assert "ai" in dimensions and "agents" in dimensions
    assert "workflows" in measures
    assert "workflows" not in dimensions
    assert "ai" not in measures and "agents" not in measures


def test_what_entity_largest_measure_variant():
    # Covers the "What <entity> has/have the largest <measure>?" phrasing
    # named in the target behavior, not just "which"/"highest"/"most".
    concepts, measures, dimensions = extract_terms("What clients have the largest orders?")
    assert "clients" in dimensions
    assert "orders" in measures


def test_existing_measure_by_dimension_behavior_unchanged():
    # Non-regression: the "by" split takes priority and is untouched.
    concepts, measures, dimensions = extract_terms("total revenue by region")
    assert measures == ["revenue"]
    assert dimensions == ["region"]
    intent = extract_query_intent("total revenue by region")
    assert intent["aggregation"] == "SUM"


def test_most_recent_still_produces_latest_order_not_max():
    # "most recent" must stay a LATEST/order signal, not collide with the
    # new "most" MAX detection.
    intent = extract_query_intent("What is the most recent invoice?")
    assert intent["aggregation"] is None
    assert intent["order"] is not None
    assert intent["order"]["target"] == "date"


def test_wh_ranking_pattern_does_not_fire_without_a_ranking_word():
    # A plain WH question with no highest/most/largest falls through to the
    # original flat-measures behavior unchanged.
    concepts, measures, dimensions = extract_terms("What is the status of clients?")
    assert dimensions == []


def test_wh_ranking_pattern_does_not_fire_without_distinct_entity():
    # "What is the highest invoice amount?" has no entity distinct from the
    # measure clause (no "which <entity> <verb> ..." structure) — stays flat.
    concepts, measures, dimensions = extract_terms("What is the highest invoice amount?")
    assert dimensions == []
    assert "invoice" in measures and "amount" in measures
