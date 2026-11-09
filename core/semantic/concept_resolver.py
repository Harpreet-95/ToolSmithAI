from __future__ import annotations

import re
from datetime import date, timedelta

from core.dictionary.rule_classifier import _tokenize

# Small, deterministic stopword set for splitting a raw business question into
# candidate concept/measure/dimension terms. Deliberately mirrors the same
# "split on 'by', drop stopwords" heuristic formerly used in the API routing
# layer. core/ must not import from api/, so this is a small, faithful
# reimplementation rather than a
# cross-layer import. No AI/ML — deterministic token heuristic only.
_STOPWORDS = frozenset({
    "show", "me", "the", "a", "an", "of", "for", "how", "many", "what",
    "is", "are", "from", "in", "on", "at", "with", "and", "or", "give",
    "get", "list", "display", "find", "tell", "which", "per", "each", "all",
    "their", "its", "using", "across", "among", "between",
    # Phase 6.4 (Enterprise Semantic Accuracy) additions — kept in sync with
    # api/v1/routes.py::_NL_STOP (see that set's own comment for the full
    # root-cause writeup: a bare grammatical/modifier word in this term list
    # pulls unrelated tables into _collect_candidate_tables()'s unioned
    # candidate pool in data/query_planning_service.py, which then dilutes
    # every real term's own column scoring — reproduced against a real production database
    # metadata via this exact function, since it is what
    # core/orchestrator/context_builder.py's live SQL_REQUEST path actually
    # calls (the former routes.py copy has been removed). Every word below already has its own
    # independent handler further down in this same file (status_value,
    # order, aggregation, date_range all run their own regex over the raw
    # question string, never over this term list).
    "this", "that", "these", "those", "there", "who", "whose",
    "have", "has", "having", "were", "was", "do", "does", "did",
    "made", "make", "worked", "linked", "added", "most", "performing",
    "to", "s", "still", "so", "number", "we", "i", "you", "they", "it",
    "active", "inactive", "open", "closed", "cancelled", "canceled", "completed", "pending",
    "latest", "newest", "earliest", "oldest", "current", "recent", "recently", "top", "bottom",
    "distinct", "unique",
    "total", "average", "highest", "lowest", "sum", "percent", "percentage", "compare",
    # Phase 4, Milestone 1 (Enterprise Semantic Accuracy) additions — same
    # imperative-instruction category as the "show"/"list"/"display"/"give"/
    # "get"/"find"/"tell" entries above, just missing until this milestone's
    # failure audit found them leaking into concept/measure search as their
    # own bogus term (e.g. "Sort clients by name" searching for a column
    # named "sort").
    "sort", "sorted", "return",
    # Day 2C follow-up ("material qualifier policy") — "database" is a
    # generic, self-referential word ("how many students are in the
    # database?") that never names a real business concept and predictably
    # fails every resolver, but was previously silently absorbed by
    # sql_planning_service.py's/context_builder.py's own "some other term
    # resolved, skip the rest" leniency. That leniency is now narrower (see
    # both modules' own updated docstrings), so a genuinely decorative word
    # like this must be filtered here instead, the same way every other
    # grammatical-filler word above already is — same category, not a new
    # exception. Reproduced against a real fixture
    # (tests/test_live_sql_qa_repro.py::test_scalar_count_students).
    # "system" is the identical case ("how many clients are in the
    # system?") — same category, same fix.
    "database", "system",
    # NOTE: bare calendar words (year/month/quarter/week/day/today/yesterday)
    # were tried and reverted — an existing fixture/test
    # (test_composer_sql_routing.py::test_date_range_filter_end_to_end)
    # proved a bare "month" is not always redundant with date_range
    # detection: it is also the only term that resolves a literal
    # "order_month" DIMENSION column when the question has no "this
    # month"/"last month" phrase for date_range to match instead. Excluding
    # calendar words broke that real, already-tested case, so unlike the
    # grammatical-filler/status/ranking/aggregation words above (each
    # verified redundant against the full backend suite, zero regressions),
    # calendar words keep their pre-Phase-6.4 behavior.
    #
    # 2026-08-09 stabilization — date-verb parity with data/
    # query_planning_service.py's own _DATE_ORDER_HINT_ROOTS. "added" above
    # was already excluded from term search, but its siblings there
    # (created/started/joined/began/ended/registered/updated/modified/
    # inserted/finished) were not, so each one survived extraction and was
    # searched as a real business concept/measure term instead of being
    # reserved purely as a date-verb hint — confirmed live against real
    # the reference database: "How many enrollments were created this month?" leaked "created"
    # in as a bogus concept competing with the real "enrollment" term.
    # ("completed"/"active"/etc. status words above already cover the
    # overlap with _DATE_ORDER_HINT_ROOTS' "completed"/"complete".)
    "started", "start", "began", "begin", "joined", "join",
    "created", "create", "registered", "inserted", "insert",
    "ended", "end", "complete", "finished", "finish",
    "updated", "update", "modified", "modify",
    # FINISH INTERACTIVE AI Q&A WIRING milestone, Part 7 — "trend"/"trends"
    # ("Show enrollment trend by month") is the identical imperative/
    # analytical-request category as "show"/"list"/"compare" above: it
    # names HOW to present the answer (a time series), never a business
    # entity, column, or table a resolver could ever legitimately match.
    # Live-confirmed against a real production database: left unstopped, it survived
    # extraction as a genuine leftover concept/measure term and tripped
    # attempt_ai_physical_plan's extra_term_implies_attribute_request_out_
    # of_stage1_scope safety net (correctly, since that check has no way to
    # know "trend" isn't a real requested column) -- the actual fix is
    # here, at the source, not a new exemption in that already-general
    # check.
    "trend", "trends",
})

def extract_terms(question: str) -> tuple[list[str], list[str], list[str]]:
    """Split a NL question into (concepts, measure_terms, dimension_terms).

    Words after "by" are dimension hints; all other content words (minus
    stopwords) become both concepts and measure candidates.

    Sprint 1.4: when there's no "by" clause, a WH-ranking question ("Which
    <entity> has/have/teach/... the highest/most/largest <measure>?") is
    tried next — the entity between the WH-word and the ranking clause
    becomes the dimension, the phrase after the ranking word becomes the
    measure. Only reached when the "by" split above finds nothing, so
    existing "measure by dimension" behavior is unchanged. Falls through to
    the original flat-measures behavior when this pattern doesn't match
    either.
    """
    # Date-range phrases ("this year", "last month", ...) are already fully
    # parsed elsewhere by extract_query_intent()/_DATE_LABEL_PATTERNS (below
    # in this same file). The calendar word of a MATCHED phrase is redundant
    # here and, worse, can spuriously column-match an unrelated table (e.g.
    # bare "year" token-matching a "YearsExpValue" column on a completely
    # unrelated table, silently selecting the wrong table for the whole
    # question — reproduced against a real production database). Only the words of a phrase
    # that actually matched are removed; a bare, unqualified calendar word
    # (no "this"/"last" prefix) is deliberately left alone, since it can be
    # the only term that resolves a literal date dimension column like
    # "order_month" when there's no "this/last" phrase for date_range to
    # match instead (see _STOPWORDS' own note above).
    _date_phrase_words: set[str] = set()
    for _label, _pattern in _DATE_LABEL_PATTERNS:
        if _pattern.search(question or ""):
            _date_phrase_words.update(_tokenize(_label.replace("_", " ")))

    # "<entity> are stalled, active, graduated, or not started?" — the
    # enumerated status/category values are predicate values, not business
    # concepts to search table names/columns for; dropped the same way
    # _date_phrase_words is, so they never leak into concept/measure
    # candidate-table search (previously the exact cause of a real
    # multi-concept join-search slowdown/refusal for this question shape).
    _status_enum_words: set[str] = set()
    for _phrase in _extract_status_enumeration(question or ""):
        _status_enum_words.update(_tokenize(_phrase))

    # "on file" / "on record" / "has an email" / "missing a phone number" —
    # see _IDIOM_PHRASES' own docstring. Only the words of a phrase that
    # actually matched are removed, same discipline as _date_phrase_words
    # above — the idiom's SUBJECT word (e.g. "email") is never in the
    # stripped label, so it stays a real term for concept/column search.
    _idiom_phrase_words: set[str] = set()
    for _label, _pattern, _polarity in _IDIOM_PHRASES:
        if _pattern.search(question or ""):
            _idiom_phrase_words.update(_tokenize(_label.replace("_", " ")))

    # CURRENT-STATE MILESTONE, Part 2 — "currently enrolled"/"still active"/
    # "current status" is a MATERIAL semantic constraint
    # (extract_query_intent's current_state_required), not a business
    # concept/column name to search for. Unlike the idiom phrases above,
    # BOTH words of the matched phrase are stripped (not just the modifier)
    # — "enrolled"/"active"/"employed"/"subscribed" have no meaning here as
    # a standalone searchable term either; they only ever named a state
    # together with "currently"/"still". Same "only the words of a phrase
    # that actually matched are removed" discipline as _date_phrase_words.
    _current_state_words: set[str] = set()
    _cs_required, _cs_phrase, _cs_subject = _detect_current_state(question or "")
    if _cs_required and _cs_phrase:
        _current_state_words.update(_tokenize(_cs_phrase))

    # A bare numeral ("10") is never a business concept/measure/dimension
    # name — it is always either a LIMIT (already parsed separately by
    # extract_query_intent()'s _TOP_N_RE/_BOTTOM_N_RE/_N_MOST_RECENT_RE) or
    # otherwise grammatically inert here. Dropped unconditionally, the same
    # way _date_phrase_words is dropped above, so a numeral never leaks into
    # business-dictionary ambiguity resolution downstream.
    words = [
        w for w in _tokenize(question or "")
        if w and w not in _date_phrase_words and w not in _status_enum_words
        and w not in _idiom_phrase_words and w not in _current_state_words and not w.isdigit()
    ]
    try:
        by_idx = words.index("by")
        after = [w for w in words[by_idx + 1:] if w not in _STOPWORDS]
        before = [w for w in words[:by_idx] if w not in _STOPWORDS]
    except ValueError:
        wh_match = _WH_RANKING_RE.match(question or "")
        each_per_match = None if wh_match else _EACH_PER_RE.search(question or "")
        of_attr_match = None if (wh_match or each_per_match) else _OF_ATTRIBUTE_RE.search(question or "")
        if wh_match:
            # entity -> dimension, measure -> measure. The verb between them
            # ("have", "teach", "executed", ...) is intentionally dropped —
            # it's grammatical scaffolding, not a business term.
            after = [
                w for w in _tokenize(wh_match.group("entity"))
                if w not in _STOPWORDS and w not in _date_phrase_words
            ]
            before = [
                w for w in _tokenize(wh_match.group("measure"))
                if w not in _STOPWORDS and w not in _date_phrase_words
            ]
        elif each_per_match:
            # "each year" / "per class" — the same role split as an explicit
            # "by" clause ("revenue by month"), just spelled with "each"/
            # "per" instead of "by". The grain/dimension word itself becomes
            # the sole dimension term; everything else becomes the
            # concept/measure term(s), exactly like the "by" branch above.
            grain_word = each_per_match.group(1).lower()
            after = [grain_word]
            before = [w for w in words if w not in _STOPWORDS and w != grain_word]
        elif of_attr_match:
            attr_word = of_attr_match.group(1).lower()
            after = [attr_word]
            before = [w for w in words if w not in _STOPWORDS and w != attr_word]
        else:
            after = []
            before = [w for w in words if w not in _STOPWORDS]

    concepts = list(dict.fromkeys(before + after))
    measures = list(dict.fromkeys(before))
    dimensions = list(dict.fromkeys(after))
    return concepts, measures, dimensions


# ---------------------------------------------------------------------------
# Enterprise Accuracy Program A2/Phase C — Compound Business Phrase Candidates
#
# extract_terms() above is intentionally left untouched: it still returns
# single-word tokens, and every existing caller keeps working byte-for-byte
# identically. This section adds a separate, additive, pure (no DB, no
# scoring) helper that proposes BOUNDED adjacent-word phrase candidates from
# an already-extracted token list (e.g. concepts=['job','orders']) for a
# caller (data/query_planning_service.py) to try resolving against real
# metadata BEFORE falling back to the individual tokens. This function only
# proposes candidates — it never decides whether a phrase is a real business
# term; that is left entirely to the existing, unmodified
# _resolve_concept()/_resolve_term()/_resolve_entity_count() confidence and
# ambiguity gates.
#
# Deliberately generic — no database-specific phrase list, no hardcoded table or
# domain names. The exclusions below fall out of properties every one of the
# spec's negative examples already has, not a lookup table:
#   - "by" clauses: extract_terms() already splits measures/dimensions across
#     "by" before this ever runs, so the two halves are never in the same
#     token list to begin with.
#   - conjunctions/commas ("clients and invoices"): the raw-adjacency check
#     below requires literally nothing (not even a dropped stopword) between
#     the two words in the ORIGINAL question text.
#   - time expressions ("last quarter"): already removed from the token list
#     by extract_terms()'s own _date_phrase_words filter before this runs.
#   - ranking/top-N language ("top 5 clients"): "top"/"bottom" are already
#     _STOPWORDS, dropped before this stage; a bare numeral is additionally
#     never allowed to participate below (numerals are never half of a real
#     business entity/measure name).
# ---------------------------------------------------------------------------

_COMPOUND_MAX_CANDIDATES_PER_QUESTION = 5


def _raw_adjacent(question: str, first: str, second: str) -> bool:
    """True iff `first` and `second` appear literally consecutive in
    `question` (case-insensitive, optionally hyphen-joined, only whitespace
    or a hyphen between them — nothing else, not a dropped stopword, not a
    conjunction, not "by"). Purely a substring/regex check against the raw
    question text; independent of any metadata."""
    if not first or not second:
        return False
    pattern = rf"\b{re.escape(first)}\b[\s-]+\b{re.escape(second)}\b"
    return re.search(pattern, question or "", re.IGNORECASE) is not None


def generate_compound_phrase_candidates(question: str, tokens: list[str]) -> list[dict]:
    """
    Propose bounded, generic, metadata-independent 2-word phrase candidates
    from an already-extracted token list (one of extract_terms()'s own
    concepts/measures/dimensions lists), for a caller to try resolving
    against real metadata before falling back to individual tokens.

    Only literally-adjacent word pairs (see _raw_adjacent) are proposed —
    never every possible pair, never a 3+-word phrase (the spec's own worked
    example for a 3-token list, ["client","job","orders"], expects exactly
    the two adjacent bigrams ["client job", "job orders"], never the trigram
    "client job orders"). A pair containing a purely-numeric token is
    excluded (never a meaningful half of a business entity/measure name).
    Deduplicated, left-to-right, capped at
    _COMPOUND_MAX_CANDIDATES_PER_QUESTION.

    Returns [{"phrase": "job orders", "components": ("job", "orders")}, ...].
    """
    candidates: list[dict] = []
    seen_phrases: set[str] = set()
    for i in range(len(tokens) - 1):
        first, second = tokens[i], tokens[i + 1]
        if first.isdigit() or second.isdigit():
            continue
        if not _raw_adjacent(question, first, second):
            continue
        phrase = f"{first} {second}"
        if phrase in seen_phrases:
            continue
        seen_phrases.add(phrase)
        candidates.append({"phrase": phrase, "components": (first, second)})
        if len(candidates) >= _COMPOUND_MAX_CANDIDATES_PER_QUESTION:
            break
    return candidates


# ---------------------------------------------------------------------------
# Enterprise Question Intelligence (Milestone M-1 / EDP M-7)
#
# Deterministic, regex-based detection of question SHAPE — aggregation,
# distinct, ranking/ordering, date range, status value. This is deliberately
# NOT business-vocabulary expansion (no new synonyms, no new domain/entity
# concepts) — it only classifies the grammatical shape of the question, the
# same class of work core/execution/rules.py already does for
# is_analytical_question/mentions_pii. No AI/LLM anywhere in this section.
# ---------------------------------------------------------------------------

# Order matters: COUNT patterns are checked before the bare SUM pattern so
# "total number of invoices" resolves to COUNT, not SUM (it contains "total"
# but the intent is a row count, not a monetary sum).
_COUNT_RE      = re.compile(r"\bhow many\b|\bnumber of\b|\bcount of\b|\btotal number of\b", re.IGNORECASE)
_SUM_RE        = re.compile(r"\btotal\b|\bsum of\b", re.IGNORECASE)
_AVG_RE        = re.compile(r"\baverage\b|\bavg\b|\bmean\b", re.IGNORECASE)
_MIN_RE        = re.compile(r"\blowest\b|\bsmallest\b|\bminimum\b|\bmin\b", re.IGNORECASE)
# "most" is a MAX-equivalent ranking word ("the most classes", "the most
# workflows") except when it precedes "recent(ly)"/"current" — that phrasing
# is reserved for _LATEST_RE's date-ordering signal below, not an
# aggregation (mirrors the existing _LEADING_FIRST_RE "not immediately
# followed by 'name'" guard further down in this file for the same kind of
# collision). The lookahead must match "recently" too, not just "recent" —
# a bare "\brecent\b" left "most recently added" incorrectly matching MAX
# (the word-boundary after "recent" never lands inside "recently"),
# misclassifying an entity-count/list question as a scalar MAX aggregate.
_MAX_RE        = re.compile(
    r"\bhighest\b|\blargest\b|\bmaximum\b|\bmax\b|\bbiggest\b|\bmost\b(?!\s+(?:recent(?:ly)?|current)\b)",
    re.IGNORECASE,
)

# Sprint 1.4 — "Which <entity> has/have/teach/executed/... the
# highest/most/largest <measure>?" ranking pattern. The single word between
# the entity and the ranking clause is a verb ("have", "teach", "executed",
# ...) and is deliberately not captured as either group. Reuses _MAX_RE's own
# ranking-word vocabulary so "most" stays in sync with the aggregation
# detector above rather than duplicating that word list. Only used by
# extract_terms() when no "by" clause is present, so it never touches the
# existing "measure by dimension" path.
# FINAL PRE-37 MILESTONE, Part 3 — "Which category has the FEWEST products?"
# is the same WH-ranking grammatical shape as the MAX case above, just the
# opposite direction. Kept as its own word set rather than added to _MIN_RE
# itself: "fewest"/"least" specifically mean "smallest COUNT of rows", never
# a scalar MIN of a stored numeric column (unlike "lowest"/"smallest"/
# "minimum", which _MIN_RE already covers for the bare-scalar-aggregate
# case elsewhere) — conflating the two would risk misclassifying an
# unrelated bare scalar-MIN question.
_WH_RANKING_MIN_WORDS_RE = re.compile(
    r"\blowest\b|\bsmallest\b|\bminimum\b|\bmin\b|\bfewest\b|\bleast\b",
    re.IGNORECASE,
)
_WH_RANKING_RE = re.compile(
    rf"^\s*(?:which|what)\s+(?P<entity>.+?)\s+\w+\s+(?:the\s+)?"
    rf"(?P<rankword>{_MAX_RE.pattern}|{_WH_RANKING_MIN_WORDS_RE.pattern})"
    rf"\s+(?P<measure>.+?)\s*\??\s*$",
    re.IGNORECASE,
)

# "each year" / "per class" — an implicit "by <dimension>" split spelled with
# "each"/"per" instead of "by" (rule A: "each year", "per class" must be
# recognized as analytical/grouping operators, not business terms). Only
# tried by extract_terms() when no "by" clause and no WH-ranking match are
# present, so it never touches either of those existing paths. Matches the
# single word immediately after "each"/"per" — the same one-word-dimension
# shape the "by" branch already produces for "revenue by month".
_EACH_PER_RE = re.compile(r"\b(?:each|per)\s+([a-z]+)\b", re.IGNORECASE)

# "names of the students" — an attribute-of-entity phrase, the mirror image
# of "revenue by month": the attribute word (immediately before "of") is the
# dimension being requested, everything else is the entity. Reuses the same
# attribute keyword data.query_planning_service._resolve_related_attribute_
# dimension recognizes ("name"/"names" only, for now — the one attribute
# this milestone's reproduction requires; not a general preposition parser).
_OF_ATTRIBUTE_RE = re.compile(r"\b(names?)\s+of\b", re.IGNORECASE)

_DISTINCT_RE   = re.compile(r"\bdistinct\b|\bunique\b", re.IGNORECASE)

_TOP_N_RE      = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_BOTTOM_N_RE   = re.compile(r"\bbottom\s+(\d+)\b", re.IGNORECASE)
# "10 most recently added" / "10 most recent" — an explicit row count named
# WITHOUT the literal word "top" (_TOP_N_RE requires "top" immediately before
# the digits, so it never matches this phrasing). Checked ahead of the bare
# _LATEST_RE branch below so the question's own explicit number wins over
# the generic default limit.
_N_MOST_RECENT_RE = re.compile(r"\b(\d+)\s+most\s+recent(?:ly)?\b", re.IGNORECASE)
_LATEST_RE     = re.compile(r"\b(latest|newest|recent(?:ly)?|current)\b", re.IGNORECASE)
# Phase 6.4 (Enterprise Semantic Accuracy) note: "recent"/"current" added as
# LATEST-equivalent synonyms. Milestone M-1 only wired "latest"/"newest"
# here; Phase 6.3 separately added "current"/"recent" to the Intent
# Resolver's SQL_REQUEST routing keywords, but never extended this order
# regex — so "Current payroll"/"Recent placements" routed correctly to
# SQL_REQUEST yet carried no actual ordering signal once inside semantic
# resolution (order stayed None). Reusing the existing LATEST branch (DESC
# by date, default limit) closes that gap with no new pattern.
# "earliest"/"oldest" anywhere, OR a leading "first" not immediately followed
# by "name" (the single most common false-positive: "first name").
_EARLIEST_RE   = re.compile(r"\b(earliest|oldest)\b", re.IGNORECASE)
_LEADING_FIRST_RE = re.compile(r"^\s*first\b(?!\s+name)", re.IGNORECASE)

# Bare singular superlative ranking with no countable "top N" phrasing
# ("Who is the top performing recruiter?", "What is the best client?") — the
# same no-count, entity-plus-implicit-measure shape _LATEST_RE/_EARLIEST_RE
# above already handle for date ranking, generalized to a non-date measure.
# Root-caused live against a real production database (Portable Business Learning Layer
# milestone, Part 0 ground-truth verification): "Who is the top performing
# recruiter?" has no digit after "top", so _TOP_N_RE never matched, `order`
# stayed None, and the downstream AI Physical Planner's own comment
# ("ai_interpretation.sorting is ignored in favor of the deterministic
# query_intent['order']") meant the AI Interpreter's own correctly-detected
# sorting intent (measures=["performance"], sorting={"by":"performance",
# "limit":1}) was silently discarded entirely -- producing an unfiltered
# bare list of every row instead of a ranking, a resolvable-uncertainty
# investigation, or a safe refusal. limit defaults to 1 (singular "the top
# X"/"the best X"), not _DEFAULT_ORDER_LIMIT (10, a LATEST/EARLIEST-style
# multi-row listing default) -- matches the AI interpreter's own
# independently-derived limit for this exact phrasing.
_BARE_TOP_SUPERLATIVE_RE = re.compile(r"\b(?:top|best)\b(?!\s*\d)", re.IGNORECASE)
_BARE_BOTTOM_SUPERLATIVE_RE = re.compile(r"\bworst\b(?!\s*\d)", re.IGNORECASE)

# Default row cap for TOP/BOTTOM/LATEST/EARLIEST when the question doesn't
# name an explicit number (e.g. "latest invoices" vs "top 10 clients").
_DEFAULT_ORDER_LIMIT = 10

_STATUS_VALUES = (
    "active", "inactive", "open", "closed", "completed", "cancelled", "pending",
    # NEXT MILESTONE, Part 1/4 — generic antonym-pair status words. Not
    # database-specific: these are common business status vocabulary (the same
    # closed-list style as the original 7 words above), added so the
    # inherently-negative pole of a pair ("unpaid", "disabled") is matched
    # exactly like "inactive" already was, and so _STATUS_NEGATION_CUE_RE
    # below has a positive-pole word ("paid", "enabled") to negate via
    # "not paid"/"not enabled".
    "paid", "unpaid", "enabled", "disabled",
)
_STATUS_ALIASES = {"canceled": "cancelled"}
_STATUS_RE = re.compile(
    r"\b(" + "|".join(_STATUS_VALUES) + "|" + "|".join(_STATUS_ALIASES) + r")\b",
    re.IGNORECASE,
)

# NEXT MILESTONE, Part 1 — root cause of "List classes that are not active"
# incorrectly generating IsActive = 1: _STATUS_RE above matches the bare word
# "active" inside "not active" (the word boundary before "active" is still
# satisfied), and nothing downstream ever looked at what came before the
# match — the "not" was silently discarded. This regex detects a negation
# cue occupying the text IMMEDIATELY (whitespace-only) before a status-word
# match: applied to the prefix substring via search()+"$"-anchoring, so it
# only fires when the cue directly precedes the matched word, never when a
# negation appears earlier in an unrelated clause ("Students who are not in
# the active program" would not — deliberately conservative, see the
# adjacent-word discipline _raw_adjacent already uses elsewhere in this
# file). Deliberately generic — no database-specific status list, works for any
# word _STATUS_RE matches (active/paid/completed/cancelled/enabled/...).
_STATUS_NEGATION_CUE_RE = re.compile(
    r"\b(?:not|isn't|isnt|aren't|arent|wasn't|wasnt|weren't|werent|"
    r"doesn't|doesnt|don't|dont|didn't|didnt|no\s+longer|never)\s*$",
    re.IGNORECASE,
)

# "<entity> are stalled, active, graduated, or not started?" — a GRAMMATICAL
# pattern (an Oxford-comma-style list of predicate values after a linking
# verb), not a hardcoded status-word vocabulary like _STATUS_VALUES above:
# works for any schema's own category/status labels, never just the ones in
# that closed list. Only treated as a genuine enumeration when the tail
# after "is"/"are" contains an actual COMMA-separated list (>=2 phrases) —
# requiring a literal comma (not just a bare "and"/"or") is what tells
# "stalled, active, graduated, or not started" (a real value list) apart
# from "clients and invoices related" (an entity conjunction sharing one
# trailing predicate, no comma at all — must stay two ordinary concept
# terms, not be shredded into fake status values). A single trailing
# predicate with no comma ("How many students are in the database?" -> tail
# "in the database") is likewise left alone.
_LINKING_VERB_LIST_RE = re.compile(r"\b(?:is|are)\s+(?P<list>.+?)\s*\??\s*$", re.IGNORECASE)
_TRAILING_CONNECTOR_RE = re.compile(r"^(?:or|and)\s+", re.IGNORECASE)


def _extract_status_enumeration(question: str) -> list[str]:
    m = _LINKING_VERB_LIST_RE.search(question or "")
    if not m:
        return []
    tail = m.group("list")
    if "," not in tail:
        return []
    raw_phrases = tail.split(",")
    phrases = []
    for i, p in enumerate(raw_phrases):
        p = p.strip()
        if i == len(raw_phrases) - 1:
            p = _TRAILING_CONNECTOR_RE.sub("", p).strip()
        if p:
            phrases.append(p)
    return phrases if len(phrases) >= 2 else []

_BETWEEN_DATES_RE = re.compile(
    r"\bbetween\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\s+and\s+(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b",
    re.IGNORECASE,
)

# Phase 4, Milestone 1 — idioms whose literal words are not the business
# concept they name. "on file"/"on record" mean "recorded/available", not a
# reference to a document/file entity — decomposed literally, "file" alone
# can confidently name-match a real (but completely unrelated) table, e.g.
# a CMS attachments table, producing a wrong answer with high confidence.
# Phrase-scoped (like _DATE_LABEL_PATTERNS/_extract_status_enumeration
# below) so only the words of a MATCHED idiom are dropped — "file"/"record"
# used as genuine business nouns elsewhere are never globally banned.
#
# NEXT MILESTONE — INTERACTIVE AI Q&A COVERAGE COMPLETION + SAFETY, Part 3 —
# widened from the original on_file/on_record pair to the general class of
# EXISTENCE/NULL business idioms ("has an email", "missing a phone number",
# "without an email", "where X exists") and each entry now also carries its
# POLARITY: "not_null" (the idiom asserts the subject attribute IS
# populated) or "null" (the idiom asserts it is ABSENT). Every entry is
# still phrase-scoped exactly like on_file/on_record above — the SUBJECT
# word after the idiom (e.g. "email") is never part of the stripped label,
# so it always survives as a real term for the caller to resolve to a
# column. Deliberately does NOT include a bare "with a/an X" pattern: "with"
# is used for genuine attribute=value filters too often in real business
# questions ("clients with open job orders", "students with an F grade") to
# treat as an unconditional existence signal from a regex alone — that
# shape is left to the AI interpreter's own (per-question, semantically
# aware) filter extraction instead, never guessed deterministically here.
# The has/have pattern excludes a following past-participle/gerund word
# ("have graduated", "have enrolled") via negative lookahead, since that is
# a status/relationship statement, not an existence qualifier.
_IDIOM_PHRASES: tuple[tuple[str, "re.Pattern[str]", str], ...] = (
    ("on_file", re.compile(r"\bon file\b", re.IGNORECASE), "not_null"),
    ("on_record", re.compile(r"\bon record\b", re.IGNORECASE), "not_null"),
    # FINAL PRE-37 MILESTONE, Part 2 — excludes a bare "the" immediately
    # after has/have (no "a"/"an" article) from the existence-idiom match.
    # Live-confirmed against a real production database: "Which location HAS THE most
    # classes?" was matching this idiom ("has" + no article + "the", which
    # the un-narrowed \w+ happily captured) and being treated as an
    # existence check ("does location have a 'the'?"), not the ranking
    # question it actually is. "has the X" is not a genuine English
    # existence idiom in the first place — the definite article "the"
    # signals a specific/already-known reference (ranking superlatives,
    # comparisons, ordinary entity mentions), never "does X exist at all"
    # (that reading uses "a"/"an" or a bare noun: "has an email"/"has
    # email"). Scoped to exactly this one word so genuine idioms with an
    # indefinite article ("has a phone number") or no article at all ("has
    # email on file") are completely unaffected.
    ("has_have", re.compile(
        r"\b(?:has|have)\s+(?:an?\s+)?(?!\w+(?:ed|ing)\b)(?!the\b)\w+", re.IGNORECASE,
    ), "not_null"),
    ("without", re.compile(r"\bwithout\s+(?:an?\s+)?", re.IGNORECASE), "null"),
    ("missing", re.compile(r"\bmissing\s+(?:an?\s+)?", re.IGNORECASE), "null"),
    ("lacking", re.compile(r"\blacking\s+(?:an?\s+)?", re.IGNORECASE), "null"),
    ("is_are_missing", re.compile(r"\b(?:is|are)\s+missing\b", re.IGNORECASE), "null"),
    ("where_exists", re.compile(r"\bwhere\s+\w+\s+exists?\b", re.IGNORECASE), "not_null"),
)

# CURRENT-STATE MILESTONE, Part 2 — "currently"/"still X"/"current X" names a
# MATERIAL current-state constraint (active/non-cancelled/non-ended/still-
# in-force), never a decorative word to search for as a business term.
# Deliberately generic — no database-specific vocabulary, and no assumption here
# about WHICH column satisfies it; that schema-driven discovery lives in
# core.answering.current_state_semantics, reached only after this function
# has already told the caller a current-state requirement exists at all.
# `subject` (group 1) is the word the modifier attaches to ("enrolled" in
# "currently enrolled", "active" in "still active") when the phrasing names
# one — checked first, since it is the more specific/informative match, the
# same "more specific pattern wins" discipline _DATE_LABEL_PATTERNS already
# documents. The bare fallback (no following word, e.g. "Are they currently
# still?" — rare) still sets the flag with subject=None rather than losing
# the signal entirely.
_CURRENT_STATE_SUBJECT_RE = re.compile(
    r"\b(?:currently|still|current)\s+([a-z]+)\b", re.IGNORECASE,
)
_CURRENT_STATE_BARE_RE = re.compile(r"\b(?:currently|still)\b", re.IGNORECASE)


def _detect_current_state(question: str) -> tuple[bool, str | None, str | None]:
    """(required, phrase, subject) for extract_query_intent's own
    current_state_required/current_state_phrase/current_state_subject
    fields — see that function's docstring."""
    q = question or ""
    m = _CURRENT_STATE_SUBJECT_RE.search(q)
    if m:
        return True, m.group(0), m.group(1).lower()
    m = _CURRENT_STATE_BARE_RE.search(q)
    if m:
        return True, m.group(0), None
    return False, None, None

# Ordered so more specific phrases ("this week") are checked before anything
# that could partially overlap a less specific one.
_DATE_LABEL_PATTERNS = (
    ("today", re.compile(r"\btoday\b", re.IGNORECASE)),
    ("yesterday", re.compile(r"\byesterday\b", re.IGNORECASE)),
    ("this_week", re.compile(r"\bthis week\b", re.IGNORECASE)),
    ("last_week", re.compile(r"\blast week\b", re.IGNORECASE)),
    ("this_month", re.compile(r"\bthis month\b", re.IGNORECASE)),
    ("last_month", re.compile(r"\blast month\b", re.IGNORECASE)),
    ("this_quarter", re.compile(r"\bthis quarter\b", re.IGNORECASE)),
    ("last_quarter", re.compile(r"\blast quarter\b", re.IGNORECASE)),
    ("this_year", re.compile(r"\bthis year\b", re.IGNORECASE)),
    ("last_year", re.compile(r"\blast year\b", re.IGNORECASE)),
)


def _parse_loose_date(raw: str) -> date | None:
    """Accepts 'YYYY-MM-DD' or 'MM/DD/YYYY'. Never guesses — returns None on
    anything else so an unparsable date is dropped rather than invented."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _quarter_bounds(d: date) -> tuple[date, date]:
    q_start_month = ((d.month - 1) // 3) * 3 + 1
    start = date(d.year, q_start_month, 1)
    end_month = q_start_month + 2
    if end_month == 12:
        end = date(d.year, 12, 31)
    else:
        end = date(d.year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def _month_bounds(d: date) -> tuple[date, date]:
    start = date(d.year, d.month, 1)
    if d.month == 12:
        end = date(d.year, 12, 31)
    else:
        end = date(d.year, d.month + 1, 1) - timedelta(days=1)
    return start, end


def _compute_date_range(label: str, today: date | None = None) -> dict | None:
    """Pure calendar-bucket math, no NL parsing here — `today` defaults to
    the real wall-clock date so production behavior always reflects the
    actual current date; tests may pass a fixed `today` for determinism."""
    today = today or date.today()

    if label == "today":
        start = end = today
    elif label == "yesterday":
        start = end = today - timedelta(days=1)
    elif label == "this_week":
        start = today - timedelta(days=today.weekday())
        end = start + timedelta(days=6)
    elif label == "last_week":
        this_week_start = today - timedelta(days=today.weekday())
        start = this_week_start - timedelta(days=7)
        end = this_week_start - timedelta(days=1)
    elif label == "this_month":
        start, end = _month_bounds(today)
    elif label == "last_month":
        last_month_day = date(today.year, today.month, 1) - timedelta(days=1)
        start, end = _month_bounds(last_month_day)
    elif label == "this_quarter":
        start, end = _quarter_bounds(today)
    elif label == "last_quarter":
        this_q_start, _ = _quarter_bounds(today)
        last_q_day = this_q_start - timedelta(days=1)
        start, end = _quarter_bounds(last_q_day)
    elif label == "this_year":
        start, end = date(today.year, 1, 1), date(today.year, 12, 31)
    elif label == "last_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    else:
        return None

    return {"label": label, "start": start.isoformat(), "end": end.isoformat()}


# ---------------------------------------------------------------------------
# Aggregation Semantics / Composed Ranking milestone, Part 2 — ranking
# DIRECTION as a signal fully independent of aggregation FUNCTION.
#
# extract_query_intent()'s own `order` field intentionally stays narrow (top
# N / bottom N / N-most-recent / latest / earliest / bare "top"|"best"|
# "worst" — see _BARE_TOP_SUPERLATIVE_RE's docstring above for why that carve
# -out exists and why it deliberately excludes highest/largest/most/lowest/
# smallest/fewest/maximum/minimum: those words are ALSO the vocabulary
# extract_query_intent uses to pick the aggregation FUNCTION for a bare
# scalar superlative ("the highest salary" -> MAX(salary), no grouping, no
# ranking needed at all). Folding them into `order` too would make every
# bare "highest X" question ALSO carry a synthetic order/limit, which is
# wrong for that shape and — verified against
# core.orchestrator.ai_physical_planning's own ranking-gate ("a ranking
# request with no dimension to group by is disqualified, never guessed") —
# would turn "largest single order" from a valid bare MAX into a safe
# refusal. That is a real, different shape from "highest TOTAL cost" (a
# GROUP BY + aggregate + rank-and-take-top-1 request), and the two must not
# be conflated by widening `order` itself.
#
# This function is the deliberately SEPARATE signal: it answers "does this
# question's wording carry ranking direction," with no opinion on whether
# ranking actually applies (that decision needs to know whether a grouping
# dimension exists, which only the caller — a bare-superlative-vs-grouped-
# aggregate branch in the physical planner — actually knows). Reuses the
# exact same vetted word lists already defined above (_MAX_RE/
# _WH_RANKING_MIN_WORDS_RE/_BARE_TOP_SUPERLATIVE_RE/
# _BARE_BOTTOM_SUPERLATIVE_RE) rather than a new, divergent word list.
# ---------------------------------------------------------------------------

def detect_ranking_direction(question: str) -> str | None:
    """Returns "DESC" for a top/highest/largest/most/best-shaped superlative,
    "ASC" for a bottom/lowest/smallest/fewest/least/worst-shaped one, or None
    when the question carries no ranking-direction wording at all.

    Deliberately independent of aggregation-function detection (a caller
    that also needs SUM/AVG/COUNT/MIN/MAX should call extract_query_intent
    separately — the two are two distinct fields by design, never
    conflated: a question's ranking direction and its aggregation function
    are orthogonal, see this milestone's fix to extract_query_intent's own
    aggregation priority order above for the bug this exact conflation
    caused). No AI/LLM, deterministic regex only.
    """
    q = question or ""
    if _MAX_RE.search(q) or _BARE_TOP_SUPERLATIVE_RE.search(q):
        return "DESC"
    if _WH_RANKING_MIN_WORDS_RE.search(q) or _BARE_BOTTOM_SUPERLATIVE_RE.search(q):
        return "ASC"
    return None


def extract_query_intent(question: str, *, today: date | None = None) -> dict:
    """Detect the SHAPE of a business question — aggregation, distinct,
    ranking/ordering, date range, status value — from the raw NL text.

    Deterministic regex only, no AI/LLM. Never invents a value it can't
    support with a direct textual match: an unmatched category returns None
    rather than a guess (e.g. an unparsable "between X and Y" date pair
    returns date_range=None instead of a fabricated range).

    Returns:
        {
          "aggregation": "COUNT"|"SUM"|"AVG"|"MIN"|"MAX"|None,
          "aggregation_target": "entity_count"|"distinct_entity_count"|"measure_sum"|
                                 "measure_average"|"measure_min"|"measure_max"|
                                 "non_null_column_count"|None,
          "distinct": bool,
          "order": {"direction": "ASC"|"DESC", "limit": int} | None,
          "date_range": {"label": str, "start": iso, "end": iso} | None,
          "status_value": "Active"|"Inactive"|...|None,
          "status_negated": bool,  # True for "not active"/"isn't active"/...
          "null_check_requested": bool,
          "null_check_polarity": "not_null"|"null"|None,
          "current_state_required": bool,  # "currently"/"still X"/"current X" — see _detect_current_state
          "current_state_phrase": str | None,  # the literal matched phrase, e.g. "currently enrolled"
          "current_state_subject": str | None,  # the word it modifies, e.g. "enrolled"; None for a bare match
        }
    """
    q = question or ""

    # Aggregation Semantics / Composed Ranking milestone, Part 1/2 — SUM
    # (and AVG, already correctly ordered before this fix) must win over a
    # bare superlative word (highest/largest/most/lowest/smallest/...) when
    # BOTH appear in the same question. Live-confirmed root cause (Fixture
    # E, "Which depot has the highest total maintenance cost?"): the old
    # order below was COUNT, AVG, MIN, MAX, SUM — an elif chain, so once
    # _MAX_RE matched "highest" the scan never reached "total" at all, and
    # aggregation came back "MAX" (a scalar max of ONE work order's cost)
    # instead of "SUM" (the actual "total" the question asked for).
    # "total"/"sum of" is an explicit, unambiguous aggregation-function
    # QUALIFIER — it must always beat a superlative word that is doing
    # RANKING work, not aggregation-function work, in the same sentence
    # (see detect_ranking_direction() below, which independently recovers
    # the ranking direction those same superlative words also carry, so
    # nothing is lost by no longer letting them win the aggregation-function
    # vote). SUM is deliberately placed in the same position AVG already
    # occupied (ahead of MIN/MAX) rather than merely swapped with MAX, so
    # "highest total X" and "highest average X" are now handled identically.
    if _COUNT_RE.search(q):
        aggregation = "COUNT"
    elif _AVG_RE.search(q):
        aggregation = "AVG"
    elif _SUM_RE.search(q):
        aggregation = "SUM"
    elif _MIN_RE.search(q):
        aggregation = "MIN"
    elif _MAX_RE.search(q):
        aggregation = "MAX"
    else:
        aggregation = None

    distinct = bool(_DISTINCT_RE.search(q))

    order: dict | None = None
    top_match = _TOP_N_RE.search(q)
    bottom_match = _BOTTOM_N_RE.search(q)
    n_recent_match = _N_MOST_RECENT_RE.search(q)
    if top_match:
        order = {"direction": "DESC", "limit": int(top_match.group(1))}
    elif bottom_match:
        order = {"direction": "ASC", "limit": int(bottom_match.group(1))}
    elif n_recent_match:
        order = {"direction": "DESC", "limit": int(n_recent_match.group(1)), "target": "date"}
    elif _LATEST_RE.search(q):
        order = {"direction": "DESC", "limit": _DEFAULT_ORDER_LIMIT, "target": "date"}
    elif _EARLIEST_RE.search(q) or _LEADING_FIRST_RE.search(q):
        order = {"direction": "ASC", "limit": _DEFAULT_ORDER_LIMIT, "target": "date"}
    elif _BARE_TOP_SUPERLATIVE_RE.search(q):
        order = {"direction": "DESC", "limit": 1}
    elif _BARE_BOTTOM_SUPERLATIVE_RE.search(q):
        order = {"direction": "ASC", "limit": 1}

    date_range = None
    between_match = _BETWEEN_DATES_RE.search(q)
    if between_match:
        start = _parse_loose_date(between_match.group(1))
        end = _parse_loose_date(between_match.group(2))
        if start and end:
            date_range = {"label": "between", "start": start.isoformat(), "end": end.isoformat()}
        # else: dates present but unparsable — leave date_range=None, never guess
    else:
        for label, pattern in _DATE_LABEL_PATTERNS:
            if pattern.search(q):
                date_range = _compute_date_range(label, today=today)
                break

    status_match = _STATUS_RE.search(q)
    status_value = None
    status_negated = False
    if status_match:
        raw = status_match.group(1).lower()
        canonical = _STATUS_ALIASES.get(raw, raw)
        status_value = canonical.capitalize()
        # NEXT MILESTONE, Part 1 — status_value stays the LITERAL matched
        # word (unchanged from before this milestone): "inactive" still
        # produces status_value="Inactive", not a normalized "Active"+
        # negated=True. Normalizing inherently-negative words would be
        # unsafe for a real TEXT status column with >2 values (e.g. Active/
        # Inactive/Pending) — "!= 'Active'" would wrongly include Pending
        # rows that "= 'Inactive'" correctly excludes. status_negated is
        # purely an ADDITIONAL signal for the "not <word>" phrasing case,
        # where no literal negative-pole word exists in the question at
        # all — see _typed_status_filter_value's own docstring downstream
        # for how the two combine.
        status_negated = bool(_STATUS_NEGATION_CUE_RE.search(q[:status_match.start()]))

    # "are stalled, active, graduated, or not started" — a genuine
    # multi-value status/category ENUMERATION (grouped-count shape),
    # distinct from status_value's single-value FILTER shape above. See
    # _extract_status_enumeration's own docstring for the grammatical
    # (not vocabulary-based) detection rule.
    status_values = _extract_status_enumeration(q)

    # Day 2C follow-up ("material qualifier policy", Part B) — "have a
    # phone number on file"/"... on record" is an implied NOT NULL
    # condition on whatever attribute the idiom modifies. extract_terms()
    # already detects and strips this idiom's own words from term search
    # (_IDIOM_PHRASES, module-level below) but previously never told any
    # caller a null-check was actually requested, so the qualifier was
    # silently dropped rather than becoming a filter — verified live
    # against a real production database ("How many clients have a phone number on file?"
    # silently counted every client). Surfaced here as a plain bool;
    # data.query_planning_service pairs it with whichever single
    # already-resolved column the idiom's subject term maps to.
    #
    # NEXT MILESTONE — Part 3 — null_check_requested now also fires for the
    # wider existence-idiom class (has/without/missing/lacking/where X
    # exists), and null_check_polarity names which direction the FIRST
    # matching idiom asserts ("not_null" or "null") — callers that only
    # ever supported the not_null case (on_file/on_record) must check
    # polarity == "not_null" before acting, so they are never silently
    # handed a "null" idiom they'd apply with the wrong sign. When more
    # than one idiom matches the same question (rare, e.g. a contradictory
    # phrasing), the first match in _IDIOM_PHRASES' own declared order wins
    # — same "more specific/earlier entry wins" discipline _DATE_LABEL_
    # PATTERNS already documents above.
    null_check_requested = False
    null_check_polarity: str | None = None
    for _label, _pattern, _polarity in _IDIOM_PHRASES:
        if _pattern.search(q):
            null_check_requested = True
            null_check_polarity = _polarity
            break

    # Milestone Phase 6.2 — Aggregation Shape Correctness. A pure relabeling
    # of the aggregation/distinct signals already detected above — no new
    # phrasing detection. COUNT is always an entity/record-cardinality
    # question ("how many X", "number of X"), never a request to sum a
    # stored metric (those use "total"/"sum of", which already route to
    # SUM above) — see query_planning_service._resolve_entity_count for
    # where this drives resolution away from column-level measure matching.
    # non_null_column_count is a modeled-but-undetected value this
    # milestone: no example in scope needs "count of non-null values in a
    # named column" distinguished from entity_count, so nothing here ever
    # produces it — left available for a future milestone rather than
    # guessed at.
    if aggregation == "COUNT":
        aggregation_target = "distinct_entity_count" if distinct else "entity_count"
    elif aggregation == "SUM":
        aggregation_target = "measure_sum"
    elif aggregation == "AVG":
        aggregation_target = "measure_average"
    elif aggregation == "MIN":
        aggregation_target = "measure_min"
    elif aggregation == "MAX":
        aggregation_target = "measure_max"
    else:
        aggregation_target = None

    current_state_required, current_state_phrase, current_state_subject = _detect_current_state(q)

    return {
        "aggregation": aggregation,
        "aggregation_target": aggregation_target,
        "distinct": distinct,
        "order": order,
        "date_range": date_range,
        "status_value": status_value,
        "status_values": status_values,
        "status_negated": status_negated,
        "null_check_requested": null_check_requested,
        "null_check_polarity": null_check_polarity,
        "current_state_required": current_state_required,
        "current_state_phrase": current_state_phrase,
        "current_state_subject": current_state_subject,
    }


# ---------------------------------------------------------------------------
# Analytics Intent Layer (Enterprise Implementation Phase 2)
#
# Recognizes the BUSINESS QUESTION SHAPE (Ranking / Aggregation / Trend /
# Comparison / Distribution) so the planner can reason about analytics shape
# before resolving individual columns. This is a pure extension layered on
# top of extract_terms()/extract_query_intent() above — neither function is
# modified, both are reused exactly as-is. No AI/LLM, deterministic regex
# only, same as every other detector in this file.
# ---------------------------------------------------------------------------

_TREND_RE = re.compile(
    r"\bover time\b|\bby month\b|\bby year\b|\bby week\b|\bby quarter\b|\bmonthly\b|"
    r"\byearly\b|\bweekly\b|\bquarterly\b|\bgrowth\b|\btrend\b|"
    r"\beach year\b|\beach month\b|\beach week\b|\beach quarter\b|"
    r"\bper year\b|\bper month\b|\bper week\b|\bper quarter\b",
    re.IGNORECASE,
)
_COMPARISON_RE = re.compile(
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|\bdifference between\b",
    re.IGNORECASE,
)
_DISTRIBUTION_RE = re.compile(
    r"\bbreakdown\b|\bgrouped by\b|\bdistribution\b",
    re.IGNORECASE,
)

# "Top 10 clients by active jobs" — the reverse order of the
# "<measure> by <dimension>" pattern extract_terms() already splits on
# ("revenue by region"): here the entity being ranked comes BEFORE "by" and
# the measure it's ranked on comes AFTER. Only used inside
# derive_analytics_intent for entity/measure picking below; does not change
# extract_terms()'s own concepts/measures/dimensions split.
_TOP_N_BY_RE = re.compile(
    r"^\s*(?:top|bottom)\s+\d+\s+(?P<entity>.+?)\s+by\s+(?P<measure>.+?)\s*\??\s*$",
    re.IGNORECASE,
)

# Trend vocabulary words that must never themselves be picked as the
# "measure" for a trend question (e.g. "Monthly enrollment trend" — the
# measure is "enrollment", not "monthly" or "trend").
_TREND_WORDS = frozenset({
    "over", "time", "monthly", "yearly", "weekly", "quarterly", "growth", "trend", "month", "year",
})


def derive_analytics_intent(question: str, *, today: date | None = None) -> dict:
    """Business Question Shape detection — runs after semantic retrieval and
    before query_planning_service resolves columns. Reuses extract_terms()
    and extract_query_intent() verbatim; adds question-shape classification
    and a row-counting default that those two functions don't compute.

    Returns:
        {
          "entity": str | None,          # the business noun being grouped/ranked
          "measure": str | None,         # the business noun being measured
          "aggregation": "COUNT"|"SUM"|"AVG"|"MIN"|"MAX"|None,
          "aggregation_target": (same values as extract_query_intent, already
                                  corrected for the ranking-defaults-to-COUNT
                                  rule below),
          "grouping": str | None,        # == entity; kept as its own key to
                                          # match the planner's GROUP BY vocabulary
          "ordering": "ASC"|"DESC"|None,
          "top_n": int | None,           # default 10 for ranking questions
          "question_type": "ranking"|"trend"|"comparison"|"distribution"|
                            "aggregation"|None,
          "confidence": "high"|"medium"|"low",
          "shape_source": "wh_ranking"|"top_n_by"|"trend"|"plain",
          # shape_source: which pattern produced entity/measure — callers
          # may trust some sources more than others; see the shape_source
          # assignment below for why "top_n_by" is the least trustworthy.
        }
    """
    concepts, measures, dimensions = extract_terms(question)
    query_intent = extract_query_intent(question, today=today)
    wh_match = _WH_RANKING_RE.match(question or "")
    top_n_by_match = _TOP_N_BY_RE.match(question or "")

    # _WH_RANKING_RE can match syntactically ("What is the highest salary?"
    # captures entity="is") without capturing a real business term — "is" is
    # pure grammatical filler that extract_terms() itself would drop as a
    # stopword. Only treat the WH-ranking pattern as an entity/measure split
    # (and therefore as a "ranking" question, i.e. GROUP BY + COUNT) when the
    # captured entity survives stopword filtering; otherwise this is a bare
    # scalar aggregate ("what is the highest salary?" -> MAX(salary), no
    # grouping) and must fall through to normal aggregation handling.
    wh_entity_tokens = [w for w in _tokenize(wh_match.group("entity")) if w not in _STOPWORDS] if wh_match else []
    wh_is_real_ranking = bool(wh_match) and bool(wh_entity_tokens)

    is_ranking = wh_is_real_ranking or bool(top_n_by_match) or bool(query_intent["order"])
    if _TREND_RE.search(question or ""):
        question_type = "trend"
    elif _COMPARISON_RE.search(question or ""):
        question_type = "comparison"
    elif is_ranking:
        question_type = "ranking"
    elif _DISTRIBUTION_RE.search(question or "") or dimensions:
        question_type = "distribution"
    elif query_intent["aggregation"]:
        question_type = "aggregation"
    else:
        question_type = None

    # shape_source records WHICH pattern produced entity/measure, so a
    # caller (query_planning_service) can choose to trust some patterns more
    # than others. "top_n_by" ("Top 10 clients by active jobs") is
    # deliberately the least trusted: the identical surface phrasing "Top 10
    # clients by revenue" is genuinely ambiguous between "rank client rows
    # by their own revenue column" (no grouping) and "rank clients by a
    # COUNT of related job records" (GROUP BY + COUNT) — resolvable only
    # after column resolution knows whether the measure lives on the same
    # table as the entity or a related one, which is one stage later than
    # this layer runs. See query_planning_service.plan_business_query's own
    # wiring comment for how each source is used.
    measure_phrase = ""
    if top_n_by_match:
        entity_tokens = [w for w in _tokenize(top_n_by_match.group("entity")) if w not in _STOPWORDS]
        measure_tokens = [w for w in _tokenize(top_n_by_match.group("measure")) if w not in _STOPWORDS]
        entity = entity_tokens[0] if entity_tokens else None
        measure = measure_tokens[0] if measure_tokens else None
        measure_phrase = top_n_by_match.group("measure") or ""
        shape_source = "top_n_by"
    elif wh_is_real_ranking:
        entity = wh_entity_tokens[0]
        measure_tokens = [w for w in _tokenize(wh_match.group("measure")) if w not in _STOPWORDS]
        measure = measure_tokens[0] if measure_tokens else None
        measure_phrase = wh_match.group("measure") or ""
        shape_source = "wh_ranking"
    elif question_type == "trend":
        # Drop trend vocabulary itself ("monthly", "trend", ...) so it's
        # never mistaken for the business measure being trended.
        measure_candidates = [w for w in measures if w not in _TREND_WORDS]
        entity_candidates = [w for w in dimensions if w not in _TREND_WORDS]
        entity = entity_candidates[0] if entity_candidates else None
        measure = measure_candidates[0] if measure_candidates else (concepts[0] if concepts else None)
        shape_source = "trend"
    else:
        entity = dimensions[0] if dimensions else None
        measure = measures[0] if measures else None
        shape_source = "plain"

    if question_type == "ranking":
        # A ranking question's aggregation is derived from the measure
        # PHRASE alone, not extract_query_intent's whole-question keyword
        # scan: that scan checks COUNT/AVG/MIN/MAX/SUM in a fixed priority
        # order over the entire sentence, so "the highest total sales"
        # matches MAX (the ranking word "highest") before it ever reaches
        # "total" (SUM) — right for a bare scalar aggregate, wrong once the
        # sentence also carries an entity/measure split ("which product...")
        # where "highest" is grammar for ranking, not the requested
        # aggregation. Defaults to COUNT (rank by row count) when the
        # measure phrase names no explicit numeric aggregation at all —
        # "highest"/"most"/"top N" ask to count/rank business records, not
        # to compute a scalar MAX/MIN of a stored column.
        if _SUM_RE.search(measure_phrase):
            aggregation, aggregation_target = "SUM", "measure_sum"
        elif _AVG_RE.search(measure_phrase):
            aggregation, aggregation_target = "AVG", "measure_average"
        elif _COUNT_RE.search(measure_phrase):
            aggregation = "COUNT"
            aggregation_target = "distinct_entity_count" if query_intent["distinct"] else "entity_count"
        else:
            aggregation = "COUNT"
            aggregation_target = "distinct_entity_count" if query_intent["distinct"] else "entity_count"
    elif question_type == "trend" and not query_intent["aggregation"]:
        # A trend question naming no explicit numeric aggregation verb
        # ("total"/"average"/...) means "count of records per time bucket"
        # (e.g. "enrollment trend by month" = count of enrollments per
        # month) — the same count-of-records default `ranking` above
        # already applies when its own measure phrase names no explicit
        # aggregation. query_intent["aggregation"] (a whole-question SUM/
        # AVG/COUNT scan) still wins first, so "total enrollment trend by
        # month" correctly stays SUM.
        aggregation = "COUNT"
        aggregation_target = "distinct_entity_count" if query_intent["distinct"] else "entity_count"
    else:
        aggregation = query_intent["aggregation"]
        aggregation_target = query_intent["aggregation_target"]

    order = query_intent["order"]
    if order:
        ordering = order["direction"]
        top_n = order.get("limit")
    elif question_type == "ranking":
        # FINAL PRE-37 MILESTONE, Part 3 — direction follows the actual
        # matched rank word ("fewest"/"lowest"/... -> ASC) rather than
        # always defaulting to DESC; only wh_ranking carries a captured
        # rankword (top_n_by's "top/bottom N" phrasing already set `order`
        # above via extract_query_intent, so this branch is wh_ranking-only
        # in practice).
        if wh_is_real_ranking and _WH_RANKING_MIN_WORDS_RE.search(wh_match.group("rankword") or ""):
            ordering = "ASC"
        else:
            ordering = "DESC"
        # Existing, already-tested contract (test_analytics_intent_layer.py):
        # a bare "which X has the most Y" with no explicit number still
        # returns up to _DEFAULT_ORDER_LIMIT (10) ranked rows, not a forced
        # single row — ties are common in real data, and the caller can
        # always read just the first row for "the" winner. An explicit
        # "top 5 locations by classes" still goes through extract_query_
        # intent's own order/limit detection above and never reaches this
        # branch.
        top_n = _DEFAULT_ORDER_LIMIT
    else:
        ordering = None
        top_n = None

    if entity and measure:
        confidence = "high"
    elif measure and question_type == "trend":
        # A bare calendar-grain trend ("<measure> trend by month/year/...")
        # has no separate business entity to group by — the time bucket
        # itself is the grouping, resolved later via the calendar-grain
        # dimension fallback, not through `entity` here (entity_candidates
        # is expected to be empty once trend words like "month" are
        # stripped from it above). Requiring `entity` too would permanently
        # cap every such question at "medium", so query_planning_service's
        # high-confidence gap-fill (including the aggregation default just
        # above) would never reach it.
        confidence = "high"
    elif entity or measure:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "entity": entity,
        "measure": measure,
        "aggregation": aggregation,
        "aggregation_target": aggregation_target,
        "grouping": entity,
        "ordering": ordering,
        "top_n": top_n,
        "question_type": question_type,
        "confidence": confidence,
        "shape_source": shape_source,
    }


# ---------------------------------------------------------------------------
# EDP AI-Native Phase 1 — Comparison baseline detection.
#
# derive_analytics_intent() above already classifies question_type=="comparison"
# via _COMPARISON_RE ("compare"/"comparison"/"versus"/"vs"/"difference
# between"), but that regex alone can't tell a caller WHICH prior period to
# compare against, and doesn't match the "compared to"/"YoY"/"previous
# period" phrasing this milestone specifically needs (verified: \bcompare\b
# requires the exact word "compare", not "compared" — "compared to last
# month" does not match _COMPARISON_RE at all). This function adds baseline
# extraction only; it does not change _COMPARISON_RE, question_type, or any
# existing derive_analytics_intent() behavior, and every existing caller of
# derive_analytics_intent is unaffected. Deterministic regex only, no AI/LLM,
# same collision-checked-narrow style as the rest of this file.
#
# "MoM"/"WoW" bare acronyms are deliberately NOT matched (unlike "YoY"/"QoQ")
# — "mom" and "wow" collide with common English words ("mom" = mother, "wow"
# = exclamation) and would false-positive on ordinary questions. The spelled-
# out "month-over-month"/"week-over-week" forms are still covered.
# ---------------------------------------------------------------------------

_YOY_RE = re.compile(r"\byear[\s-]over[\s-]year\b|\byoy\b", re.IGNORECASE)
_QOQ_RE = re.compile(r"\bquarter[\s-]over[\s-]quarter\b|\bqoq\b", re.IGNORECASE)
_MOM_SPELLED_RE = re.compile(r"\bmonth[\s-]over[\s-]month\b", re.IGNORECASE)
_WOW_SPELLED_RE = re.compile(r"\bweek[\s-]over[\s-]week\b", re.IGNORECASE)

# "vs/versus/compared to/compared with (the) prior|previous|last <grain>" —
# captures which time grain the comparison is against. Two capture groups
# (not one alternation) because the "compared to the previous month" and
# bare "previous month" shapes have different leading context; group index
# tells the caller nothing extra, so detect_comparison_intent() below just
# takes whichever one matched.
_PRIOR_GRAIN_RE = re.compile(
    r"\b(?:vs\.?|versus|compared to|compared with)\s+(?:the\s+)?(?:prior|previous|last)\s+(year|quarter|month|week)\b"
    r"|\b(?:prior|previous)\s+(year|quarter|month|week)\b",
    re.IGNORECASE,
)
_GENERIC_PRIOR_PERIOD_RE = re.compile(r"\b(?:prior|previous)\s+period\b", re.IGNORECASE)


def detect_comparison_intent(question: str) -> dict | None:
    """Detect a period-over-period comparison request and, where the text
    supports it, which baseline/grain to compare against.

    Returns None when the question carries no comparison language at all.
    Otherwise returns {"requested": True, "baseline": ..., "grain": ...}
    where baseline is one of "year_over_year"/"quarter_over_quarter"/
    "month_over_month"/"week_over_week"/"previous_period"/"unspecified", and
    grain (when known) is "year"|"quarter"|"month"|"week". "unspecified" is
    the fallback for _COMPARISON_RE's own generic words ("compare X and Y",
    a same-period comparison with no time baseline at all) — surfaced so a
    caller can still flag requires_insights without inventing a baseline it
    can't support.
    """
    q = question or ""
    if _YOY_RE.search(q):
        return {"requested": True, "baseline": "year_over_year", "grain": "year"}
    if _QOQ_RE.search(q):
        return {"requested": True, "baseline": "quarter_over_quarter", "grain": "quarter"}
    if _MOM_SPELLED_RE.search(q):
        return {"requested": True, "baseline": "month_over_month", "grain": "month"}
    if _WOW_SPELLED_RE.search(q):
        return {"requested": True, "baseline": "week_over_week", "grain": "week"}

    grain_match = _PRIOR_GRAIN_RE.search(q)
    if grain_match:
        grain = (grain_match.group(1) or grain_match.group(2) or "").lower() or None
        return {"requested": True, "baseline": "previous_period", "grain": grain}

    if _GENERIC_PRIOR_PERIOD_RE.search(q):
        return {"requested": True, "baseline": "previous_period", "grain": None}

    if _COMPARISON_RE.search(q):
        return {"requested": True, "baseline": "unspecified", "grain": None}

    return None


# FINAL LEGACY SIDE-CHANNEL RETIREMENT MILESTONE — _table_ref and
# resolve_concepts (the raw-term-to-metadata resolver backing the now-deleted
# core.semantic.planner.SemanticQueryPlanner) were removed here: zero
# remaining callers anywhere in the codebase once SemanticQueryPlanner itself
# was deleted. extract_terms/extract_query_intent/derive_analytics_intent
# above are unrelated and unaffected — they remain the shared deterministic
# primitives the AI-first pipeline and plan_business_query's own deterministic
# construction still use.
