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
import inspect
from datetime import date

from core.semantic.concept_resolver import (
    extract_terms, extract_query_intent, generate_compound_phrase_candidates,
)

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
# extract_terms() — imperative verbs and the "on file"/"on record" idiom
# excluded (Phase 4, Milestone 1)
#
# Root cause (Phase 4 failure audit, 2026-08-04): "sort"/"sorted"/"return"
# are imperative instruction words, the same grammatical category as the
# already-stopworded "show"/"list"/"display"/"give"/"get"/"find"/"tell" —
# but were missing from _STOPWORDS, so they leaked in as their own bogus
# concept/measure term and searched for a matching column, dragging in an
# unrelated table via the same dilution mechanism test_ranking_words_excluded
# above already guards against for "latest"/"highest"/etc. "on file"/
# "on record" is a two-word idiom meaning "recorded/available" — decomposed
# literally, its own word "file" coincidentally name-matches real tables
# (e.g. a CMS attachments table), producing a confident but completely
# wrong answer. Fixed the same way _date_phrase_words/_status_enum_words
# above are: phrase-level detection on the raw question, so only "file"/
# "record" occurring in this exact idiom are dropped — "file" used as a
# genuine business noun elsewhere must still survive.
# ---------------------------------------------------------------------------

def test_sort_verb_excluded():
    concepts, measures, dimensions = extract_terms("Sort clients by name")
    assert "sort" not in concepts
    assert "sort" not in measures
    assert "clients" in concepts
    assert dimensions == ["name"]


def test_sorted_verb_excluded():
    concepts, measures, dimensions = extract_terms("Sorted by client")
    assert "sorted" not in concepts
    assert "sorted" not in measures
    assert dimensions == ["client"]


def test_return_verb_excluded():
    concepts, measures, _ = extract_terms("Return active invoices")
    assert "return" not in concepts
    assert "return" not in measures
    assert "invoices" in concepts


def test_on_file_idiom_excluded():
    # "number" is already excluded by the pre-existing _STOPWORDS entry
    # (unrelated to this fix — see its own "how many"/"number of" note);
    # only "file" is new here.
    concepts, _, _ = extract_terms("Clients with phone number on file")
    assert "file" not in concepts
    assert "clients" in concepts
    assert "phone" in concepts


def test_on_record_idiom_excluded():
    concepts, _, _ = extract_terms("Clients with phone number on record")
    assert "record" not in concepts
    assert "clients" in concepts
    assert "phone" in concepts


def test_genuine_file_entity_question_preserves_file():
    """"file" is only stripped as part of the literal "on file" idiom — a
    real question about file/document entities must keep it as a usable
    concept term."""
    concepts, _, _ = extract_terms("Show client files")
    assert "file" in concepts or "files" in concepts


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

def test_this_year_phrase_excludes_year_but_date_range_still_detected():
    # Phase 2 fix: "year" in a MATCHED "this year"/"last year" phrase is
    # dropped from concepts/measures (not just "this") — a bare "year" token
    # was reproduced spuriously column-matching an unrelated table (e.g.
    # "YearsExpValue") on a real production database data, silently selecting the wrong table
    # for the whole question. date_range detection is untouched (it parses
    # the raw question independently), so "this_year" is still recognized.
    concepts, _, _ = extract_terms("Total revenue this year")
    assert "this" not in concepts
    assert "year" not in concepts
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


# ---------------------------------------------------------------------------
# Enterprise Accuracy Program A2/Phase C —
# generate_compound_phrase_candidates() — pure, no-DB phrase-generation rules
# only. Whether a proposed phrase is a real business term is decided
# elsewhere (data/query_planning_service.py, against real metadata); these
# tests only cover which candidates are PROPOSED.
# ---------------------------------------------------------------------------

def test_compound_candidate_generated_for_adjacent_job_orders():
    # (1) "job orders" is considered as one compound phrase.
    concepts, _, _ = extract_terms("How many open job orders are there?")
    assert concepts == ["job", "orders"]
    candidates = generate_compound_phrase_candidates("How many open job orders are there?", concepts)
    assert {"phrase": "job orders", "components": ("job", "orders")} in candidates


def test_three_token_list_yields_only_adjacent_bigrams_not_a_trigram():
    # Spec's own worked example: ["client","job","orders"] -> exactly the two
    # adjacent bigrams, never the trigram "client job orders", and never a
    # non-adjacent pair like "client orders".
    tokens = ["client", "job", "orders"]
    candidates = generate_compound_phrase_candidates("client job orders", tokens)
    assert [c["phrase"] for c in candidates] == ["client job", "job orders"]
    assert "client job orders" not in [c["phrase"] for c in candidates]
    assert "client orders" not in [c["phrase"] for c in candidates]


def test_compound_candidates_bounded_per_question():
    # Maximum compound candidates per question is a small bounded limit.
    tokens = ["a", "b", "c", "d", "e", "f", "g", "h"]
    question = " ".join(tokens)
    candidates = generate_compound_phrase_candidates(question, tokens)
    assert len(candidates) <= 5


def test_revenue_by_department_not_combined_into_one_phrase():
    # (8) "by" already splits measures/dimensions into separate lists before
    # this stage ever runs, so the two halves are never in the same token
    # list to be paired against each other.
    concepts, measures, dimensions = extract_terms("Revenue by department")
    assert measures == ["revenue"]
    assert dimensions == ["department"]
    assert generate_compound_phrase_candidates("Revenue by department", measures) == []
    assert generate_compound_phrase_candidates("Revenue by department", dimensions) == []


def test_time_phrase_last_quarter_not_treated_as_business_compound():
    # (9) Matched time-phrase words are already removed by extract_terms()
    # before the token list is built, so they never reach this stage.
    concepts, _, _ = extract_terms("Placements last quarter")
    assert concepts == ["placements"]
    assert generate_compound_phrase_candidates("Placements last quarter", concepts) == []


def test_clients_and_invoices_remains_two_distinct_concepts():
    # (10) A conjunction between the two words means they are never literally
    # adjacent in the original question text, so no compound merges them —
    # regardless of what else in the sentence survives as its own term.
    question = "How are clients and invoices related?"
    concepts, _, _ = extract_terms(question)
    assert "clients" in concepts and "invoices" in concepts
    phrases = [c["phrase"] for c in generate_compound_phrase_candidates(question, concepts)]
    assert "clients invoices" not in phrases


def test_ranking_phrase_top_5_clients_remains_unchanged():
    # (11) "top" is already a stopword; the bare numeral left behind is
    # excluded from ever forming a compound (numerals are never half of a
    # real business entity/measure name).
    question = "Top 5 clients by revenue"
    concepts, measures, dimensions = extract_terms(question)
    assert generate_compound_phrase_candidates(question, measures) == []


def test_filter_phrase_active_invoices_remains_compatible():
    # (12) A single surviving token (the status adjective was already
    # dropped upstream) cannot form a pair — no compound proposed, no
    # change to the existing filter-phrase flow.
    concepts, _, _ = extract_terms("Active invoices")
    assert concepts == ["invoices"]
    assert generate_compound_phrase_candidates("Active invoices", concepts) == []


def test_no_reference_specific_phrase_mapping_in_production_source():
    # (15) The generator must be generic/metadata-driven — no hardcoded
    # phrase-to-table dictionary and no database-specific vocabulary literal
    # actually compiled into the function (comments/docstrings are excluded
    # from co_consts, so illustrative examples in comments don't trip this;
    # a real hardcoded whitelist would necessarily appear as a compiled
    # string/collection constant).
    code = generate_compound_phrase_candidates.__code__
    doc = generate_compound_phrase_candidates.__doc__ or ""
    literal_consts = [c for c in code.co_consts if c != doc]
    flattened = " ".join(str(c).lower() for c in literal_consts)
    for banned in ("exampleco", "job order", "student enrollment", "edu_", "bullhorn"):
        assert banned not in flattened
