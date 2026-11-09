"""
Day 2B — Automatic Business Semantic Grounding.

Persists a versioned "semantic contract" per fixed business-entity name
(Student, Invoice, ...), resolving entity name -> ONE preferred table/view
plus curated business semantics (status/date/measure columns, verified
status values, trusted relationships, excluded derivative objects) — so the
planner can skip broad candidate search entirely for a grounded entity
instead of re-deriving the same answer on every question.

Nothing here is a new resolution engine: discovery composes exactly the
same functions the query-time planner already uses (bounded candidate
retrieval, data.query_planning_service._resolve_concept — already hardened
by the Day 2A trust/specificity guard — status/date/measure classifiers,
knowledge_graph_service.get_related_tables), just run once per entity and
persisted, instead of re-run on every question. Fully automatic — no
approval step, never routed through governance_state_map.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from data.db import get_connection

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixed business-entity taxonomy (Day 2B target entities). A constant, same
# idiom as query_planning_service._STATUS_KEYWORDS/_NEGATIVE_NAME_TOKENS —
# never learned/stored, ships with the product.
# ---------------------------------------------------------------------------

ENTITY_TAXONOMY: dict[str, list[str]] = {
    "Student":            ["student", "students"],
    "Enrollment":         ["enrollment", "enrollments"],
    "Course":             ["course", "courses"],
    "Candidate":          ["candidate", "candidates"],
    "Recruiter":          ["recruiter", "recruiters"],
    "Placement":          ["placement", "placements"],
    "Client":             ["client", "clients"],
    "Job Order":          ["job order", "job orders"],
    "Invoice":            ["invoice", "invoices"],
    "Pilot Participant": ["launch participant", "launch participants"],
}

TARGET_ENTITIES: tuple[str, ...] = tuple(ENTITY_TAXONOMY.keys())


# ---------------------------------------------------------------------------
# Automatic Multi-Entity Semantic Grounding — entity discovery is no longer
# limited to the fixed ENTITY_TAXONOMY above (see match_dynamic_entity_terms
# below). entity_synonyms() is the one seam every ENTITY_TAXONOMY[...]
# .get(...) lookup in this module (and data.query_planning_service) now goes
# through, so a dynamically-discovered entity_name — never registered in
# ENTITY_TAXONOMY — gets the exact same synonym-driven discovery/scoring
# machinery a fixed-taxonomy entity already gets. No separate code path, no
# hardcoded per-entity list.
# ---------------------------------------------------------------------------

def _derive_generic_synonyms(entity_name: str) -> tuple[str, ...]:
    """Generic singular/plural synonym pair for a term with no curated
    ENTITY_TAXONOMY entry. Pure string transform — mirrors data.vocabulary_
    service.normalize_term's own singularization rule (strip a bare
    trailing "s") so the same term normalizes identically everywhere in
    this codebase. Never a hardcoded business word; entity_name is always
    the literal term the question itself used."""
    base = (entity_name or "").strip().lower()
    if not base:
        return ()
    singular = base[:-1] if base.endswith("s") and len(base) > 3 else base
    plural = singular + ("es" if singular.endswith(("s", "x", "z", "ch", "sh")) else "s")
    return tuple(dict.fromkeys([singular, plural]))


def _term_lookup_keys(term: str) -> list[str]:
    """Normalized-key candidates for a raw term, most-likely-correct first:
    a raw "-es"-stripped form (kept as a defensive fallback for plural
    shapes normalize_term's own bare-trailing-"s" rule still doesn't cover,
    e.g. words ending in a silent "e" + "s" like "cases" already reduce
    correctly via that rule), then normalize_term's own stemming — which,
    as of the sibilant "-es" fix, already normalizes "classes" -> "class"
    directly — then the raw lowercased term. Mirrors validate_ai_entity_
    hypotheses's own local _lookup_keys helper (kept separate — that one is
    intentionally private to AI-hypothesis validation; this one is the
    dynamic-entity-detection equivalent)."""
    from data.vocabulary_service import normalize_term

    raw = (term or "").strip().lower()
    keys: list[str] = []
    if raw.endswith("es") and len(raw) > 3:
        es_stripped = raw[:-2]
        if es_stripped:
            keys.append(es_stripped)
    normalized = normalize_term(term)
    if normalized and normalized not in keys:
        keys.append(normalized)
    if raw and raw not in keys:
        keys.append(raw)
    return keys


def entity_synonyms(entity_name: str) -> tuple[str, ...]:
    """Synonym list for `entity_name` — the curated ENTITY_TAXONOMY list
    when one exists, else a generic singular/plural fallback for a
    dynamically-discovered entity (see match_dynamic_entity_terms). Every
    lookup in this module/query_planning_service that used to index
    ENTITY_TAXONOMY directly now goes through this function, so a dynamic
    entity is never a special case downstream."""
    fixed = ENTITY_TAXONOMY.get(entity_name)
    if fixed:
        return tuple(fixed)
    return _derive_generic_synonyms(entity_name)

# Day 2E, Task 4 — every resolution_status that represents a COMPLETED
# discovery attempt with no usable canonical table: NO_SAFE_SELECTION (a
# full evidence-gathering + AI adjudication + deterministic re-validation
# pass positively confirmed no reliable table) and NO_CANDIDATE (the
# deterministic pass alone found nothing — get_or_build_entity_contract's
# own metadata_revision freshness check already guarantees this reflects a
# finished attempt for the CURRENT revision, not a pending/in-progress
# state: there is no such state in this pipeline, discovery is always
# synchronous). A third literal status for "validation-rejected with no
# valid alternative" was considered but doesn't exist separately in this
# codebase — that case already produces NO_SAFE_SELECTION (see
# discover_entity_contract's fail-closed validation downgrade). Real
# production data motivating this: Placement's persisted contract is
# NO_CANDIDATE (AI adjudication disabled in that environment, so it can
# never escalate to NO_SAFE_SELECTION) — before this, it silently fell
# through the unsupported-entity fast exit entirely and hit the full
# legacy path (broad search, AI interpretation, a physical-table
# clarification picker) on every ask, live-confirmed up to 55.7s.
UNSUPPORTED_CONTRACT_STATUSES = frozenset({"NO_SAFE_SELECTION", "NO_CANDIDATE"})


def _is_confirmed_dead_end(contract: dict) -> bool:
    """A NO_CANDIDATE contract's own evidence text is NOT enough to tell
    apart two real, materially different situations that both produce the
    identical "did not clear the auto-select confidence/margin gate"
    message: real production data shows Placement (top score 0.57 vs 0.45)
    and Candidate (top score 0.47 vs 0.46) have structurally identical
    evidence shapes, yet one (Placement) is a hopeless, noisy near-miss and
    the other (Candidate) is a genuine, answerable 2-way tie a user could
    meaningfully pick between — text-matching the evidence string cannot
    distinguish them.

    The confidence SCORE the deterministic pass already computed can: a top
    candidate that doesn't even clear the ordinary auto-select confidence
    floor (query_planning_service._AUTO_SELECT_MIN_CONFIDENCE) was never a
    real match to begin with (Candidate's 0.47, Recruiter's 0.00) — that's
    a genuine dead end, not a close call. A top candidate that DOES clear
    the floor but loses only on margin against a real, close runner-up
    (Placement's 0.57) is a genuine, answerable ambiguity and must keep
    flowing to the ordinary clarification path, never be silently folded
    into "no data model exists at all."

    NO_SAFE_SELECTION is always a confirmed dead end regardless — it only
    exists after a materially stronger evidence pass (AI adjudication +
    deterministic re-validation), a different and stronger judgment than
    this deterministic-only score.
    """
    if contract.get("resolution_status") != "NO_CANDIDATE":
        return True
    from data.query_planning_service import _AUTO_SELECT_MIN_CONFIDENCE
    return (contract.get("confidence") or 0.0) < _AUTO_SELECT_MIN_CONFIDENCE


def _has_validated_alternative_evidence(
    entity_name: str, source_id: int, *, session=None,
) -> bool:
    """Before trusting a zero-confidence NO_CANDIDATE as a confirmed dead
    end, check whether this entity's own synonym terms have evidence the
    entity-contract's OWN discovery pass never considers (it scores purely
    on table/column name-matching — data.query_planning_service._resolve_
    concept called with no concept_mappings/generated_vocab at all): an
    APPROVED concept-table mapping (a human/system-confirmed promotion,
    data.concept_mapping_service — always validated by definition, since
    promotion is itself the approval step), or a generated-vocabulary
    suggestion whose OWN target table is dictionary-approved.

    Deliberately narrow: an unapproved-table generated-vocabulary
    suggestion is NOT enough on its own (weak, auto-generated, never
    human-reviewed) to defer a fast exit — only evidence that ultimately
    lands on an approved/validated business object does. A term with
    neither kind of evidence at all (the common case) returns False fast,
    with zero extra DB reads beyond the two already-indexed, source-wide
    bulk reads below.
    """
    from data.concept_mapping_service import get_all_approved_mappings
    from data.vocabulary_bootstrap_service import get_generated_vocabulary

    synonyms = entity_synonyms(entity_name)
    approved_mappings = get_all_approved_mappings(source_id, session=session)
    if any(approved_mappings.get(s) for s in synonyms):
        return True

    vocab = get_generated_vocabulary(source_id, session=session)
    candidate_tables = {row["table_fqn"] for s in synonyms for row in (vocab.get(s) or []) if row.get("table_fqn")}
    if not candidate_tables:
        return False

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        placeholders = ",".join("?" * len(candidate_tables))
        rows = conn.execute(
            f"SELECT 1 FROM data_dictionary_tables "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders}) AND is_approved = 1",
            (source_id, *candidate_tables),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    return bool(rows)


def _tokens_near_match(a: str, b: str) -> bool:
    """Same narrow, non-stemming tolerance as
    query_planning_service._tokens_near_match (duplicated rather than
    imported: that function is private to a module this one must not
    depend on internals of beyond its already-public _resolve_concept)."""
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 3 and longer.startswith(shorter) and len(longer) - len(shorter) <= 2


def _term_matches_synonym(term: str, synonym: str) -> bool:
    term_words = term.lower().split()
    syn_words = synonym.lower().split()
    if len(term_words) != len(syn_words):
        return False
    return all(_tokens_near_match(tw, sw) for tw, sw in zip(term_words, syn_words))


# ---------------------------------------------------------------------------
# Specialized Concept Principle (Final Demo Hardening, Part 2/3) — a matched
# entity's own richer, previously-grounded metadata must never silently
# absorb a question that names a MORE SPECIFIC concept just because nothing
# yet grounds the specific one. "mentorship programs" bare-matches Program
# via the single word "programs" (see match_entities_for_terms) exactly the
# same way plain "programs" does — but "mentorship" is a real, surviving
# content term sitting immediately next to that match in the raw question
# text, and it names no part of Program's own vocabulary. Confirmed live
# against a real source: without this check, apply_grounding's
# fully_grounded flag came back True from the bare "programs" hit alone,
# which made plan_business_query skip broad candidate retrieval entirely
# (query_planning_service.py's "if grounding and grounding['fully_grounded']"
# branch) and seed candidates with ONLY Program's already-grounded canonical
# table (dbo.EDU_Track) — so the cold, ungrounded, but lexically far-stronger
# dbo.EDU_Mentoring_Program was never even a candidate, regardless of how
# well it would have scored. This is the generic, symmetric fix: detecting
# the leftover modifier (not any hardcoded entity/table name) demotes
# fully_grounded for THIS question so the broad-search path still runs
# (unchanged elsewhere) and unions in the grounded table as an extra trusted
# seed (the existing partial-grounding behavior a few lines below in
# apply_grounding) — the generic table stays in the running, it just no
# longer silently wins by exclusion.
# ---------------------------------------------------------------------------

_MODIFIER_WORD_RE = re.compile(r"[a-z0-9']+")


def _find_unconsumed_adjacent_modifier(
    entity_name: str, question: str, all_terms: list[str],
) -> tuple[str, int] | None:
    """Core finder shared by _entity_match_has_unconsumed_modifier (below)
    and data.semantic_contract_service/core.orchestrator.ai_physical_
    planning's specialize_entity_term. Returns (modifier_word, position) —
    position is the modifier's index relative to the matched entity word
    (-1 = immediately before, +1 = immediately after) — for the FIRST
    single-word synonym match that has a genuine unconsumed neighbor, or
    None when `entity_name` was named only as a bare concept.

    See _entity_match_has_unconsumed_modifier's docstring for the exact,
    deliberately narrow matching rules (single-word synonym only, neighbor
    must be a surviving content term, neighbor must not itself be part of
    the entity's own name). Pure — no DB access.
    """
    own_words = {w for syn in entity_synonyms(entity_name) for w in syn.split()}
    terms_lower = {t.lower() for t in all_terms}
    words = _MODIFIER_WORD_RE.findall((question or "").lower())
    for synonym in entity_synonyms(entity_name):
        if " " in synonym:
            continue  # a matched multi-word phrase already IS the specific concept
        for i, w in enumerate(words):
            if not _tokens_near_match(w, synonym):
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(words):
                    neighbor = words[j]
                    if neighbor in terms_lower and not any(
                        _tokens_near_match(neighbor, ow) for ow in own_words
                    ):
                        return neighbor, (j - i)
    return None


def _entity_match_has_unconsumed_modifier(entity_name: str, question: str, all_terms: list[str]) -> bool:
    """True when `entity_name` was matched via a BARE single-word synonym
    hit (e.g. "program" from "programs") that sits immediately next to
    another extracted content word, in the RAW question text, that is not
    itself part of the entity's own name (e.g. "mentorship" in "mentorship
    programs"). See the module-level note above for why this exists.

    Deliberately narrow, symmetric to every other check in this module:
      - Only fires for a single-word synonym match. A matched multi-word
        synonym phrase (e.g. "job order") already names the complete,
        specific concept by itself — nothing left to be more specific about.
      - The neighbor must be a genuine SURVIVING content term (present in
        `all_terms`, i.e. it made it through extract_terms()'s own stopword/
        date-phrase/status-enumeration/idiom filtering) — a stopword or
        connector the raw text happens to contain next to the match (e.g.
        "are"/"the"/"by") never counts.
      - The neighbor must not itself be one of the entity's own synonym
        words — two adjacent words that both name the SAME entity (rare, but
        e.g. a plural + the bare noun) are not a specializing modifier.

    Pure — no DB access, no ambiguity introduced for a question that only
    ever named the bare entity.
    """
    return _find_unconsumed_adjacent_modifier(entity_name, question, all_terms) is not None


def specialize_entity_term(entity_name: str, question: str, all_terms: list[str]) -> str:
    """AI Physical Planner counterpart to apply_grounding's fully_grounded
    demotion above — same Specialized Concept Principle, different consumer.
    core.orchestrator.ai_physical_planning resolves a SINGLE AI-provided
    entity label (e.g. "program") directly via gather_candidate_evidence,
    with no equivalent of apply_grounding's broad-search-skip to demote —
    so a modifier the AI interpreter itself already dropped (LLM output is
    not guaranteed to preserve every qualifying word; live-confirmed:
    "mentorship programs" -> AIQuestionInterpretation(entities=('program',))
    for a real source) would otherwise search for the bare generic
    concept with no signal anything was lost.

    Returns `entity_name` widened to include the unconsumed adjacent
    modifier (joined in the order the two words actually appear in the raw
    question, e.g. "mentorship program"), or `entity_name` UNCHANGED when
    no such modifier is found — never invents a phrase the question doesn't
    literally contain, and a question that only ever named the bare concept
    (e.g. plain "programs") is completely unaffected.
    """
    found = _find_unconsumed_adjacent_modifier(entity_name, question, all_terms)
    if found is None:
        return entity_name
    modifier, position = found
    return f"{modifier} {entity_name}" if position < 0 else f"{entity_name} {modifier}"


def match_entities_for_terms(
    question: str, concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
) -> list[str]:
    """Every target entity named by this question, in first-appearance
    order. Checks both the deterministic term lists (single-word synonyms,
    singular/plural tolerant) and the raw question text (word-boundary
    match — needed for multi-word synonyms like "job order", which
    extract_terms may split into separate single-word terms before this
    function ever sees them). Pure function, no DB access.
    """
    lower_question = (question or "").lower()
    all_terms = list(dict.fromkeys([*concepts, *measure_terms, *dimension_terms]))
    matched: list[str] = []
    for entity_name, synonyms in ENTITY_TAXONOMY.items():
        hit = False
        for synonym in synonyms:
            if " " in synonym:
                if re.search(r"\b" + re.escape(synonym) + r"\b", lower_question):
                    hit = True
                    break
            else:
                if any(_term_matches_synonym(t, synonym) for t in all_terms):
                    hit = True
                    break
        if hit:
            matched.append(entity_name)
    # Order by first appearance in the question text (falls back to
    # taxonomy order for a synonym found only via the term list, not the
    # raw text, which should not happen in practice but stays deterministic
    # either way).
    def _first_index(entity_name: str) -> int:
        for synonym in ENTITY_TAXONOMY[entity_name]:
            idx = lower_question.find(synonym)
            if idx >= 0:
                return idx
        return len(lower_question) + 1
    matched.sort(key=_first_index)
    return matched


def entity_consumed_terms(entity_name: str, terms: list[str]) -> set[str]:
    """Day 2E, Task 2 — every term (from any role's own extracted-term list)
    that is part of `entity_name`'s own name, at word granularity.

    extract_terms() splits a multi-word entity name ("Pilot Participant")
    into separate single-word tokens ("launch", "participants") before
    query_planning_service ever sees them; match_entities_for_terms above
    still finds the entity fine (it scans the RAW QUESTION TEXT for the
    whole synonym phrase), but nothing previously told a caller which of
    the split single-word tokens are "spent" by that match, so the same
    tokens could also be independently resolved (and mis-scored) as
    unrelated measure/dimension terms. A term is consumed when it near-
    matches (same tolerance as everywhere else here) any single WORD of any
    of the entity's own synonyms — covers both the split-token case and the
    entity's synonym phrase surviving intact as one already-combined term.
    Pure — no DB access, no ambiguity introduced: a generic attribute word
    like "status" is never a word of any entity's own name, so it is never
    consumed here regardless of context.
    """
    words = {w for synonym in entity_synonyms(entity_name) for w in synonym.split()}
    return {t for t in terms if any(_tokens_near_match(t.lower(), w) for w in words)}


def match_entity_for_terms(
    question: str, concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
) -> str | None:
    """Single-entity convenience wrapper over match_entities_for_terms —
    returns the first matched entity, or None. Used by the Task 3/Task 6
    single-entity short-circuits; multi-entity callers (Task 5) use
    match_entities_for_terms directly."""
    matched = match_entities_for_terms(question, concepts, measure_terms, dimension_terms)
    return matched[0] if matched else None


# ---------------------------------------------------------------------------
# AI-Native Phase 2, Part 5 — AI entity hypotheses are validated against
# real per-source evidence before they ever reach the planner, instead of
# being merged in on the model's word alone.
# ---------------------------------------------------------------------------


def _structural_query_terms() -> frozenset[str]:
    """Words that are structural QUERY LANGUAGE (time grain, ranking,
    aggregation, status/filter language) rather than candidate business
    entities — live-confirmed defect this guards against: "Total fees paid
    by month" has deterministic dimension term "month"; generated vocabulary
    happens to contain an unrelated view with "Month" in its name
    (dbo.vw_MentorCommunication_Month), which without this exclusion reads
    as real per-source evidence that "month" is a competing entity and
    wrongly rejects the AI's correct "payment" hypothesis.

    Reuses core.semantic.concept_resolver._STOPWORDS verbatim — the same
    deterministic stopword set extract_terms() already relies on, which
    already covers most of this category (status words like "active"/
    "pending", ranking words like "top"/"most"/"highest", aggregation words
    like "total"/"average"/"sum"). Only unions in the handful of words that
    module's own docstring explains are DELIBERATELY excluded from
    _STOPWORDS for an unrelated reason (a bare "month" must survive
    extract_terms() so it can still resolve a literal order_month-style
    DIMENSION column when no date-range phrase is present) — that reason is
    about what counts as a valid dimension/measure term, not about what
    counts as trustworthy CONTRADICTION EVIDENCE against an AI entity
    hypothesis, so it does not apply here. Never mutates or duplicates
    _STOPWORDS itself.
    """
    from core.semantic.concept_resolver import _STOPWORDS

    return _STOPWORDS | {
        # Calendar/time-grain words (concept_resolver._STOPWORDS's own
        # comment on why these are excluded there does not apply to this
        # use).
        "year", "quarter", "month", "week", "day", "today", "yesterday",
        # Same relative-time/ranking/aggregation category as _STOPWORDS's
        # "current"/"top"/"bottom"/"most"/"highest"/"lowest"/"total"/
        # "average", just not literally present in that set.
        "last", "previous", "next", "least", "count",
    }


# ---------------------------------------------------------------------------
# Automatic Multi-Entity Semantic Grounding, Part 2 — a term this question
# uses that names NO fixed ENTITY_TAXONOMY entry may still name a real,
# distinct business entity. The taxonomy is a curated head start, not the
# ceiling of what this system can ground (live-confirmed gap: "instructor"
# has no taxonomy entry, so "Show enrollments and instructors" only ever
# grounds Enrollment — Instructor silently falls back to ambiguous
# column-level measure/dimension scoring instead of being recognized as a
# second entity and joined to).
# ---------------------------------------------------------------------------

def match_dynamic_entity_terms(
    source_id: int,
    concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
    already_matched: list[str],
    *, session=None,
) -> list[str]:
    """Candidate entity NAMES (e.g. "Instructor") for terms this question
    uses that name no fixed ENTITY_TAXONOMY entry, evaluated against this
    source's own already-aggregated generated business vocabulary (data.
    vocabulary_bootstrap_service — table-name/dictionary-business-name/
    domain-and-entity-assignment evidence, no new evidence source, no
    per-entity hardcoding).

    A term qualifies as a candidate when it has real TABLE-LEVEL evidence:
      - confidence_tier is HIGH or MEDIUM (LOW is "generic noise word" per
        that module's own tier docstring — never a candidate), AND
      - at least one such row has column_name == "" — the evidence names a
        TABLE, not merely a column that happens to share this word (e.g. an
        unrelated table's InstructorID column must never alone promote
        "instructor" to entity status; it is exactly the EDU_Instructor
        table's own name/dictionary-business-name evidence that does).

    Excludes terms already consumed by an already-matched entity's own name
    (entity_consumed_terms) and structural query language (time grain/
    ranking/aggregation/status words — _structural_query_terms, the same
    exclusion validate_ai_entity_hypotheses already applies to AI
    hypotheses, reused verbatim here).

    Returns CANDIDATES only — apply_grounding still runs the exact same
    get_or_build_entity_contract confirmation (table-name/authority scoring
    + auto-select confidence/margin gate) before anything here ever becomes
    a grounded entity; a candidate that fails that confirmation is silently
    dropped, never surfaced as "unsupported" (it was only ever a
    hypothesis, not a term the fixed taxonomy positively named as an
    entity).

    Pure additive optimization: never raises, returns [] on any vocabulary-
    read failure or when there are no terms to evaluate.
    """
    try:
        from data.vocabulary_bootstrap_service import get_generated_vocabulary

        vocabulary = get_generated_vocabulary(source_id, session=session)
    except Exception:  # noqa: BLE001
        logger.debug(
            "match_dynamic_entity_terms: vocabulary read failed for source_id=%s", source_id, exc_info=True,
        )
        return []
    if not vocabulary:
        return []

    all_terms = list(dict.fromkeys([*concepts, *measure_terms, *dimension_terms]))
    if not all_terms:
        return []

    consumed: set[str] = set()
    for entity_name in already_matched:
        consumed |= entity_consumed_terms(entity_name, all_terms)

    structural = _structural_query_terms()
    candidates: list[str] = []
    seen_normalized: set[str] = set()
    for term in all_terms:
        term_lower = (term or "").lower().strip()
        if not term_lower or term in consumed or term_lower in structural:
            continue
        for key in _term_lookup_keys(term):
            if key in seen_normalized:
                break
            rows = vocabulary.get(key) or []
            has_table_level_evidence = any(
                row.get("confidence_tier") in ("HIGH", "MEDIUM") and not row.get("column_name")
                for row in rows
            )
            if has_table_level_evidence:
                seen_normalized.add(key)
                candidates.append(key.title())
                break
    return candidates


def validate_ai_entity_hypotheses(
    source_id: int,
    ai_entities: list[str],
    deterministic_terms: list[str],
    *,
    single_entity_question: bool = False,
    session=None,
) -> tuple[str, ...]:
    """An AI-proposed entity (core.semantic.ai_interpreter
    .AIQuestionInterpretation.entities) is a HYPOTHESIS, not a fact — the
    model only sees a coarse, closed known-vocabulary hint
    (core.semantic.ai_interpreter.build_grounding_vocabulary, itself sourced
    from data.entity_service's fixed keyword taxonomy) and, told to "prefer
    known terms", names the nearest hint it has even when the question's own
    literal wording already names something more specific the taxonomy has
    no entry for. Live-confirmed against a real production database: "List instructors" ->
    entities=["user"]; "classes ... location" -> entities=["course"] — the
    taxonomy has no Instructor/Class entry, so the model substitutes the
    nearest generic neighbor it was told about, and that wrong entity was
    previously merged straight into the planner's concept list, sometimes
    hijacking the whole candidate pool. Both live defects share one more
    trait: the model itself reported relationship_intent="single_entity" —
    it believes the WHOLE question is about the one entity it named, which
    is exactly when a wrong entity does the most damage and exactly the
    signal `single_entity_question` is meant to carry from the caller
    (core.orchestrator.context_builder passes
    ai_interpretation.relationship_intent == "single_entity").

    Validates each AI entity against data.vocabulary_bootstrap_service's
    per-source AUTO-GENERATED vocabulary (table-name/column-name/dictionary/
    entity-assignment evidence already aggregated at bootstrap time — no new
    evidence source, no hardcoded entity list, nothing database-specific):

      - An AI entity that merely restates a term already in
        `deterministic_terms` (normalize_term-equal) is a no-op either way
        and passes through unchanged.
      - When `single_entity_question` is true (the AI itself asserts this
        is the question's only entity), REJECTED if some OTHER
        deterministic term the user actually typed already grounds
        (confidence_tier HIGH/MEDIUM) to a DIFFERENT physical table than
        the AI entity's own best match — the term the question literally
        names outranks an AI substitution for it. This is what stops
        "instructor" -> "user" / "class" -> "course" from silently
        hijacking the candidate pool. Deliberately NOT applied when the AI
        itself reports a multi-entity relationship_intent
        (list_with_related/scoped_count/scoped_aggregate): a second,
        genuinely additional entity (e.g. "recruiter" alongside
        "client"/"job order") legitimately grounds to a different table
        than terms already present — that is not a hijack, and rejecting
        it would defeat the entire point of letting the AI contribute a
        real entity the deterministic pass missed.
      - REJECTED if the AI entity itself has no real evidence at all
        (absent from the generated vocabulary, or only LOW/generic-noise
        tier) — an unsupported hypothesis must never narrow the candidate
        pool. Applied unconditionally, regardless of relationship_intent.
      - Otherwise ACCEPTED.

    Never raises. On any failure to read the generated vocabulary, every AI
    entity is accepted unchanged (today's behavior) — this function only
    ever REMOVES entities it can positively show are contradicted or
    unsupported, never on absence of evidence about evidence.
    """
    if not ai_entities:
        return tuple(ai_entities or ())

    try:
        from data.vocabulary_bootstrap_service import get_generated_vocabulary
        from data.vocabulary_service import normalize_term

        vocabulary = get_generated_vocabulary(source_id, session=session)
    except Exception:  # noqa: BLE001
        logger.debug(
            "validate_ai_entity_hypotheses: vocabulary read failed for source_id=%s", source_id, exc_info=True,
        )
        return tuple(ai_entities)

    if not vocabulary:
        return tuple(ai_entities)

    def _lookup_keys(term: str) -> list[str]:
        # normalize_term now handles the sibilant "-es" plural directly
        # ("classes" -> "class", matching the table-name-token-derived
        # vocabulary key EDU_Class actually produced). This local fallback
        # tries the normalized form first, then (for words ending in "es")
        # a raw "es"-stripped form, then the raw lowercased term itself —
        # kept as defense-in-depth for plural shapes normalize_term still
        # doesn't cover, a generic English-plural fallback, not a
        # database-specific one, scoped only to this lookup.
        raw = (term or "").strip().lower()
        keys: list[str] = []
        normalized = normalize_term(term)
        if normalized:
            keys.append(normalized)
        if raw.endswith("es") and len(raw) > 3:
            es_stripped = raw[:-2]
            if es_stripped and es_stripped not in keys:
                keys.append(es_stripped)
        if raw and raw not in keys:
            keys.append(raw)
        return keys

    def _best(term: str) -> dict | None:
        for key in _lookup_keys(term):
            rows = vocabulary.get(key)
            if rows:
                return rows[0]
        return None

    det_normalized = {(normalize_term(t) or t.lower()) for t in deterministic_terms}
    structural_terms = _structural_query_terms()

    accepted: list[str] = []
    for entity in ai_entities:
        normalized_entity = normalize_term(entity) or entity.lower()
        if normalized_entity in det_normalized:
            accepted.append(entity)
            continue

        best_ai = _best(entity)

        if single_entity_question:
            # NEXT MILESTONE — INTERACTIVE AI Q&A COVERAGE COMPLETION +
            # SAFETY, Part 5/6 (D4b) — a MULTI-WORD AI entity ("class
            # signup") is split by extract_terms() into separate single-
            # word deterministic terms ("class", "signups") before this
            # function ever sees them (same splitting behavior documented
            # on data.semantic_contract_service.entity_consumed_terms
            # above). Live-confirmed against a real production database: "How many class
            # signups are there?" rejected the AI's own entity="class
            # signup" (real MEDIUM-confidence vocabulary evidence pointing
            # at dbo.EDU_ClassSignups) purely because "class" -- the FIRST
            # WORD OF THAT SAME ENTITY NAME -- also independently grounds
            # to dbo.EDU_Class, a different physical table. That is not a
            # genuine contradicting SECOND term (the "instructor"->"user"
            # hijack this whole check exists to catch): it is the entity's
            # own name being split back into its components and then
            # compared against itself. entity_consumed_terms already
            # provides the exact "which raw terms are this entity's own
            # words" answer used elsewhere in this codebase for the
            # identical splitting problem -- reused here rather than a
            # second near-match implementation. A det_term this entity's
            # own name already accounts for can never be evidence AGAINST
            # that same entity.
            own_words = entity_consumed_terms(entity, deterministic_terms)
            contradicted = False
            for det_term in deterministic_terms:
                det_normalized_term = normalize_term(det_term) or det_term.lower()
                if det_normalized_term == normalized_entity:
                    continue
                if det_term in own_words:
                    continue
                if det_normalized_term in structural_terms or det_term.strip().lower() in structural_terms:
                    # Structural query language (time grain/ranking/
                    # aggregation/status) is never a candidate business
                    # entity — it must not be trusted as contradiction
                    # evidence against an AI hypothesis, no matter what the
                    # generated vocabulary happens to say about it.
                    continue
                best_det = _best(det_term)
                if best_det is None or best_det["confidence_tier"] not in ("HIGH", "MEDIUM"):
                    continue
                if best_ai is None or best_det["table_fqn"] != best_ai["table_fqn"]:
                    contradicted = True
                    break
            if contradicted:
                continue

        if best_ai is not None and best_ai["confidence_tier"] in ("HIGH", "MEDIUM"):
            accepted.append(entity)

    return tuple(accepted)


# ---------------------------------------------------------------------------
# Persistence — versioned, never overwritten (same idiom as
# schema_snapshots.snapshot_version): a rebuild always inserts a new
# contract_version row; readers take MAX(contract_version).
# ---------------------------------------------------------------------------

def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


def persist_entity_contract(
    source_id: int, entity_name: str, *,
    canonical_table_fqn: str | None, preferred_view_fqn: str | None,
    resolution_status: str, confidence: float, contract_json: dict, metadata_revision: str,
    generation_method: str = "auto_grounding",
    session=None,
) -> dict:
    """Insert a new contract_version row (never updates an existing one).
    Caller-scoped: does not verify source ownership itself (callers are
    always already inside an ownership-verified request).

    generation_method distinguishes Day 2B's purely deterministic
    "auto_grounding" contracts from Day 2C's "ai_adjudicated" ones —
    defaults to "auto_grounding" so every existing caller is unaffected."""
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        prior = conn.execute(
            "SELECT MAX(contract_version) AS v FROM semantic_entity_contracts "
            "WHERE source_id = ? AND entity_name = ?",
            (source_id, entity_name),
        ).fetchone()
        next_version = (prior["v"] or 0) + 1
        now = _now()
        conn.execute(
            "INSERT INTO semantic_entity_contracts "
            "(source_id, entity_name, contract_version, canonical_table_fqn, preferred_view_fqn, "
            " resolution_status, confidence, contract_json, metadata_revision, generation_method, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, entity_name, next_version, canonical_table_fqn, preferred_view_fqn,
             resolution_status, confidence, json.dumps(contract_json), metadata_revision, generation_method,
             now, now),
        )
        conn.commit()
    finally:
        if own_connection:
            conn.close()
    return {
        "source_id": source_id, "entity_name": entity_name, "contract_version": next_version,
        "canonical_table_fqn": canonical_table_fqn, "preferred_view_fqn": preferred_view_fqn,
        "resolution_status": resolution_status, "confidence": confidence,
        "contract": contract_json, "metadata_revision": metadata_revision,
        "generation_method": generation_method,
        "created_at": now, "updated_at": now,
    }


def get_latest_entity_contract(source_id: int, entity_name: str, *, session=None) -> dict | None:
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        row = conn.execute(
            "SELECT * FROM semantic_entity_contracts "
            "WHERE source_id = ? AND entity_name = ? "
            "ORDER BY contract_version DESC LIMIT 1",
            (source_id, entity_name),
        ).fetchone()
    finally:
        if own_connection:
            conn.close()
    if row is None:
        return None
    row = dict(row)
    row["contract"] = json.loads(row.pop("contract_json") or "{}")
    return row


# ---------------------------------------------------------------------------
# Task 1 — Targeted Real-Schema Discovery.
#
# Every step below composes an ALREADY-EXISTING function; this module adds
# no new resolution algorithm. See the module docstring and the Day 2B plan
# for the reuse rationale of each one.
# ---------------------------------------------------------------------------

_DATE_ROLE_BY_HINT_ROOT = {"insert": "created", "start": "started", "end": "completed", "update": "updated"}
_MAX_ALTERNATE_CANDIDATES = 3

# Day 2C, Task 2 — AI-assisted candidate adjudication bounds. "No more than
# the top 10 candidate objects" / "fully inspect no more than the top 5" per
# the task brief.
_MAX_ADJUDICATION_POOL = 10
_MAX_ADJUDICATION_INSPECTED = 5

# AI Schema Retrieval Gap fix (role-aware column-evidence promotion) — a
# measure/attribute term's correct table is sometimes evidenced ONLY by a
# column-level generated_vocabulary/concept_table_mappings match (e.g.
# "course fee" -> dbo.EDU_Class.CourseFee, zero lexical relation to the
# table's own name). _resolve_concept's own candidates[:5] per-term slice is
# sized for entity/table-identity resolution and can silently drop such a
# table when a handful of unrelated table-NAME matches outscore it lexically
# (live-confirmed against a real production database: 3 tables merely containing "course" in
# their name outranked EDU_Class's real 0.4644 score before it ever reached
# gather_candidate_evidence's own merge). Bounded small and deterministic,
# same discipline as data/semantic_retrieval_service.py's own
# _GENERATED_VOCAB_COLUMN_TOKEN_LIMIT reserved lane.
_MAX_COLUMN_EVIDENCE_PROMOTIONS_PER_TERM = 3
_MAX_EVIDENCE_COLUMNS = 20
_MAX_EVIDENCE_RELATIONSHIPS = 8
_MAX_SAMPLE_VALUES = 15

# 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-VIEW safety.
# A VIEW candidate's own definition text is the only generic, engine-neutral
# signal that it represents a filtered business SLICE (a hardcoded WHERE
# literal, TOP/LIMIT row-capping) rather than the full population for its
# apparent entity concept — live-confirmed against dbo.vw_EDU_Enrollments,
# whose body contains "WHERE AP.TrackID IN (1000)" and silently covers roughly half
# of the true dbo.EDU_ClassSignups population. Bounded to inspected
# candidates only (already capped at _MAX_ADJUDICATION_INSPECTED), one
# view_definition + one row_count investigation per VIEW candidate — the
# same per-candidate live-probe budget status_value_live_probe already
# spends elsewhere in this same function.
_MAX_LINEAGE_CANDIDATES = 2
_MAX_RESTRICTION_PROBES = 3

# PERFORMANCE PHASE C — bounded worker count for the VIEW-probe prefetch
# batch below (gather_candidate_evidence's own inspect loop). The candidate
# set this batch ever draws from is already capped at
# _MAX_ADJUDICATION_INSPECTED (5); a small, explicit, centralized bound
# smaller than that (rather than "one worker per candidate") is deliberate
# -- Part 6 of the milestone brief: "choose the smallest worker count that
# captures most of the wall-time savings," not maximum parallelism.
_MAX_VIEW_PROBE_WORKERS = 4
_VIEW_RESTRICTION_WHERE_LITERAL_RE = re.compile(
    r"\bWHERE\b(?:(?!\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b).)*?"
    r"(?:=\s*'[^']*'|=\s*\d+(?:\.\d+)?|\bIN\s*\(\s*(?:'[^']*'|\d+))",
    re.IGNORECASE | re.DOTALL,
)
_VIEW_RESTRICTION_ROW_LIMIT_RE = re.compile(r"\bTOP\s*\(?\s*\d+|\bLIMIT\s+\d+", re.IGNORECASE)
_VIEW_FROM_ROOT_TABLE_RE = re.compile(
    r"\bFROM\s+([\[\`\"]?[A-Za-z0-9_]+[\]\`\"]?(?:\s*\.\s*[\[\`\"]?[A-Za-z0-9_]+[\]\`\"]?){0,2})",
    re.IGNORECASE,
)
# View Semantic Discovery milestone — same identifier shape as
# _VIEW_FROM_ROOT_TABLE_RE above, generalized to every FROM/JOIN clause
# (not just the first FROM) so a view's full dependency set can be
# recovered, not just its structural lineage root.
_VIEW_REFERENCED_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([\[\`\"]?[A-Za-z0-9_]+[\]\`\"]?(?:\s*\.\s*[\[\`\"]?[A-Za-z0-9_]+[\]\`\"]?){0,2})",
    re.IGNORECASE,
)


def _detect_view_restriction(definition_text: str | None) -> dict | None:
    """Best-effort, generic (no table/view names hardcoded) scan of a VIEW's
    own body for evidence it filters to a business SLICE rather than
    representing its full apparent population. Returns None when there's no
    definition text to examine (e.g. dialect doesn't expose one, or the
    candidate isn't a VIEW) — absence of evidence is never treated as
    evidence of restriction."""
    if not definition_text:
        return None
    kinds: list[str] = []
    if _VIEW_RESTRICTION_WHERE_LITERAL_RE.search(definition_text):
        kinds.append("where_literal_filter")
    if _VIEW_RESTRICTION_ROW_LIMIT_RE.search(definition_text):
        kinds.append("row_limit")
    return {"restriction_detected": bool(kinds), "restriction_kinds": kinds}


def _extract_lineage_root_table(definition_text: str | None) -> str | None:
    """Returns a schema.table-shaped guess at the FIRST FROM-clause table in
    a VIEW's own definition text — the broader object this view is
    structurally built FROM. This is a syntactic guess only (a join alias, a
    CTE name, or a cross-database reference can all produce a string here);
    the caller is responsible for treating it as unverified until a real
    get_table_business_context lookup confirms it actually exists in this
    source's own catalog. Never used to build executable SQL."""
    if not definition_text:
        return None
    m = _VIEW_FROM_ROOT_TABLE_RE.search(definition_text)
    if not m:
        return None
    raw = re.sub(r"[\[\]\`\"]", "", m.group(1))
    parts = [p.strip() for p in raw.split(".") if p.strip()]
    if len(parts) < 2:
        return None
    return f"{parts[-2]}.{parts[-1]}"


def _extract_all_referenced_tables(
    definition_text: str | None, known_table_fqns: set[str] | None = None,
) -> list[str]:
    """View Semantic Discovery milestone — generalization of
    _extract_lineage_root_table above: returns every schema.table-shaped
    guess at a table referenced via a FROM or JOIN clause in a VIEW's own
    definition text, not just the first. Same syntactic-guess caveat
    applies (a join alias, a CTE name, or a cross-database reference can
    all produce a string here). When known_table_fqns is given (the
    source's real, already-discovered catalog), only guesses that actually
    resolve to a real table_fqn (case-insensitive) are returned — the
    regex is never trusted alone. Never used to build executable SQL."""
    if not definition_text:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for m in _VIEW_REFERENCED_TABLE_RE.finditer(definition_text):
        raw = re.sub(r"[\[\]\`\"]", "", m.group(1))
        parts = [p.strip() for p in raw.split(".") if p.strip()]
        if len(parts) < 2:
            continue
        fqn = f"{parts[-2]}.{parts[-1]}"
        key = fqn.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(fqn)
    if known_table_fqns is None:
        return found
    known_lower = {k.lower(): k for k in known_table_fqns}
    return [known_lower[f.lower()] for f in found if f.lower() in known_lower]


def _is_date_column(col: dict) -> bool:
    dic = col.get("dictionary") or {}
    prof = col.get("profiling") or {}
    return bool(dic.get("is_date")) or prof.get("semantic_type") == "DATE"


def _classify_date_columns(ctx: dict) -> dict[str, dict]:
    """{"created"|"started"|"completed"|"updated": {"column_name": ...}} —
    reuses query_planning_service._DATE_ORDER_HINT_ROOTS' own word->root
    vocabulary (the same list that already classifies "added"/"started"/
    "completed"/"updated" question words), applied here to COLUMN NAMES
    instead of question text, rather than maintaining a second date-word
    list."""
    from core.dictionary.rule_classifier import _tokenize
    from data.query_planning_service import _DATE_ORDER_HINT_ROOTS

    roles: dict[str, dict] = {}
    for col in ctx.get("columns", []):
        if not _is_date_column(col):
            continue
        tokens = _tokenize(col["column_name"])
        for tok in tokens:
            root = _DATE_ORDER_HINT_ROOTS.get(tok)
            if root:
                role = _DATE_ROLE_BY_HINT_ROOT[root]
                roles.setdefault(role, {"column_name": col["column_name"]})
                break
    return roles


def _sampled_status_values(source_id: int, table_fqn: str, column_name: str, *, session=None) -> list[str]:
    """Bounded, cache-only read of already-collected profiling samples for
    one column — no live DB connection. Mirrors query_planning_service.
    _cached_status_value_coverage's own JOIN, but enumerates values instead
    of checking coverage of one required value."""
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        rows = conn.execute(
            "SELECT DISTINCT pvs.value FROM profiling_value_samples pvs "
            "JOIN profiling_column_profiles pcp ON pvs.profiling_column_profile_id = pcp.id "
            "WHERE pcp.source_id = ? AND pcp.table_fqn = ? AND pcp.column_name = ? "
            "AND pvs.value IS NOT NULL",
            (source_id, table_fqn, column_name),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    return [r["value"] for r in rows]


# ---------------------------------------------------------------------------
# FINAL PRE-37 MILESTONE, Part 6/7/8/9 — Related-Value Filter resolution.
#
# "Show classes at the Richardson location": the leftover-term heuristic
# (core.orchestrator.ai_physical_planning's extra_term_implies_attribute_
# request check) has no concept of "this leftover word is a FILTER VALUE on
# a related entity", only "the AI forgot to name a requested column". Fixing
# this generically requires knowing whether "Richardson" actually EXISTS as
# a value somewhere plausible -- metadata alone (dictionary/profiling
# classification) can say a column is TEXT and categorical, but not whether
# THIS specific literal value is really in it. Live-confirmed against real
# the reference database: dbo.EDU_Location was only ever STRUCTURALLY profiled (no column-
# value statistics were ever computed for City/Location_1/Location_2), so
# profiling_value_samples (the cache _sampled_status_values above already
# reads) is empty for every candidate column on this exact table -- a real,
# necessary, narrowly-scoped live check, not a shortcut around existing
# metadata-only discipline.
# ---------------------------------------------------------------------------


def validate_categorical_value_exists(
    source_id: int, user_id: str, table_fqn: str, column_name: str, value: str, *, session=None,
) -> str | None:
    """The smallest safe live-data check this codebase performs: does one
    specific, already-user-typed literal value exist anywhere in one
    already-metadata-validated categorical column?

    Returns the value's CANONICAL, actually-stored casing (a non-empty
    string, always truthy) on a confirmed match, or None otherwise --
    callers that only need a yes/no signal can keep using this in a plain
    `if result:` check unchanged, but callers building a SQL filter (see
    core.orchestrator.ai_physical_planning._resolve_ai_categorical_value_
    filter) MUST use the returned string, not the original `value` they
    passed in, as the filter's actual comparison value: `value` is
    whatever casual casing the user typed ("enterprise"), which a
    case-INSENSITIVE confirmation (below) can match without being the
    same literal string the column actually stores ("Enterprise") -- a
    downstream case-SENSITIVE `WHERE column = ?` filter built from the
    user's original casing would then silently match zero rows.

    This is NOT a general "let the planner query arbitrary business rows"
    capability -- every input is already trusted before this function is
    ever called: table_fqn/column_name must already be a real, schema-
    validated candidate the CALLER resolved from metadata evidence first
    (see _resolve_candidate_value_columns in core.orchestrator.
    ai_physical_planning), and `value` is always the literal substring the
    USER typed in their own question text, never anything an AI model
    invented (the AI must never supply raw SQL or a raw value to search
    for here — see that module's own filter-value resolution for the
    boundary).

    Executes exactly ONE parameterized, read-only, EXISTS-shaped query:
        SELECT TOP 1 1 FROM <table> WHERE <column> = ?
    (dialect-correct TOP/LIMIT and identifier quoting via data.sql_dialects
    — the SAME dialect mechanism data.sql_generation_service.generate_sql
    already uses, never reinvented) — never string-interpolated, never a
    full-table scan across unrelated columns, never AI-authored SQL. The
    matched row's own contents are never read back or returned — only
    existence (True/False) — so no PII or business data content can ever
    leak into an AI prompt or a log from this check.

    Gated by the SAME connection-level live_query_enabled authorization
    data.query_execution_service._check_live_query_authorized already
    enforces for the composer/ask path (reused, not reimplemented) —
    returns None (not False) when the connection isn't authorized for live
    queries, or the check itself fails for any reason (connection error,
    timeout, unsupported dialect), so callers can distinguish "checked,
    value doesn't exist" from "couldn't check" and must safely disqualify
    either way rather than guessing. Never raises.

    Logged via the SAME data.query_execution_service.log_query_execution
    audit trail every other governed query uses, tagged execution_kind=
    "investigation" — the SAME already-established, already-rate-limit-
    exempt kind data.investigation_service's own internal probes use
    (_check_user_rate_limit only ever counts "user_query"), so a value-
    validation probe is fully traceable in the request record without
    counting against the user's own query rate limit or colliding with it.
    """
    from data.query_execution_service import (
        _check_live_query_authorized, _load_source_connection, log_query_execution,
    )
    from data.sql_dialects import get_adapter, source_type_to_dialect

    if not table_fqn or not column_name or not value:
        return None
    if not _check_live_query_authorized(source_id, user_id):
        return None

    try:
        db_conn, source_type = _load_source_connection(source_id, user_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "validate_categorical_value_exists: connection failed [source_id=%s]", source_id,
        )
        return None

    execution_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    canonical_value: str | None = None
    status = "failed"
    try:
        adapter = get_adapter(source_type_to_dialect(source_type))
        schema, _, table = table_fqn.partition(".")
        qualified = adapter.qualified_name(schema, table) if table else adapter.qualified_name(table_fqn)
        col = adapter.quote_identifier(column_name)
        placeholder = adapter.param_placeholder()
        sql = (
            f"SELECT {adapter.row_limit_prefix(1)}1 AS hit FROM {qualified} "
            f"WHERE {col} = {placeholder} {adapter.row_limit_suffix(1)}"
        ).strip()
        cursor = db_conn.cursor()
        cursor.execute(sql, (value,))
        if cursor.fetchone() is not None:
            canonical_value = value
        else:
            # AI-First System-Wide Intelligence milestone, Part 7D --
            # `value` is the literal substring the USER typed in casual
            # business language ("enterprise accounts"), which routinely
            # differs in case from however the source data actually stores
            # a categorical value ("Enterprise"). An exact, case-sensitive
            # match is the right FIRST check (strongest possible evidence,
            # zero false-positive risk), but failing it should not be
            # confused with "the value genuinely doesn't exist" -- a
            # case-insensitive exact match is still a full, unambiguous
            # value match (never a partial/fuzzy one), still fully
            # parameterized (LOWER() is standard ANSI SQL, safe across
            # every dialect this adapter layer supports), and still
            # requires the SAME caller-side "exactly one confirmed
            # candidate, else refuse" discipline to trust it. Selects the
            # column itself (not just a literal 1) so the caller can use
            # the value's REAL stored casing, not the user's typed casing,
            # in the downstream SQL filter it builds.
            sql_ci = (
                f"SELECT {adapter.row_limit_prefix(1)}{col} AS matched_value FROM {qualified} "
                f"WHERE LOWER({col}) = LOWER({placeholder}) {adapter.row_limit_suffix(1)}"
            ).strip()
            cursor.execute(sql_ci, (value,))
            row = cursor.fetchone()
            canonical_value = row[0] if row is not None else None
        status = "success"
    except Exception:  # noqa: BLE001
        logger.warning(
            "validate_categorical_value_exists: probe failed [source_id=%s table=%s column=%s]",
            source_id, table_fqn, column_name, exc_info=True,
        )
        canonical_value = None
        status = "failed"
    finally:
        try:
            db_conn.close()
        except Exception:  # noqa: BLE001
            pass
        duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        try:
            log_query_execution(
                execution_id, user_id, source_id, None, {"from": {"table_fqn": table_fqn}},
                param_count=1, row_count=1 if canonical_value is not None else 0, truncated=False,
                duration_ms=duration_ms, status=status,
                error_code=None if status == "success" else "value_validation_failed",
                executed_at=started_at.isoformat(), execution_kind="investigation",
            )
        except Exception:  # noqa: BLE001
            pass

    return canonical_value


def _probe_view_row_count(source_id: int, user_id: str, table_fqn: str, *, reason: str, session=None):
    """Request-scoped memoized wrapper around inspect_targeted_values's
    "row_count" live probe for one VIEW candidate table.

    PERFORMANCE PHASE B — forensic finding: the SAME VIEW table_fqn can
    surface as a candidate for more than one term/role in a single
    question (e.g. a shared VIEW appearing in both "enrollment" and
    "instructor" candidate pools), and gather_candidate_evidence's own
    inspect loop and _discover_entity_contract_deterministic's restricted-
    view check both independently probe it -- live-measured at 9-22
    row_count+view_definition probe PAIRS per question, ~1000ms/row_count
    probe, accounting for 46-60% of total latency on VIEW-heavy questions.

    session, when given, memoizes this exact live probe per
    (source_id, table_fqn) for the life of the request -- user_id is
    deliberately not part of the key (one RequestMetadataSession is always
    scoped to exactly one (source_id, user_id) pair already, same
    convention as _verified_status_values_for_column's key below).

    The FIRST call for a given table_fqn always executes the real,
    unmodified probe (inspect_targeted_values, unchanged, same safety
    checks, same bounded SQL template). A later identical call within the
    same request reuses that exact InvestigationResult object -- same
    valid/sample_values/warnings -- never re-interpreted. A failed/invalid
    probe (timeout, permission, malformed) is cached exactly like a
    successful one, for the SAME reason _verified_status_values_for_column
    already does this: within one request a repeated identical live call
    would be expected to fail the same way again (same connection, same
    permissions, same table), so this only mirrors existing semantics, it
    does not introduce a new "trust a stale failure" risk. Nothing here is
    ever persisted across requests -- the cache dies with the session.
    """
    from data.investigation_service import inspect_targeted_values

    def _compute():
        return inspect_targeted_values(
            source_id, user_id, table_fqn, None, investigation_type="row_count", reason=reason,
        )

    if session is None:
        return _compute()

    from core.perf import stage_timer

    before_hits = session.request_context_hits
    result = session.get_or_compute(f"view_row_count_probe:{source_id}:{table_fqn}", _compute)
    if session.request_context_hits > before_hits:
        stage_timer.record("view_row_count_probe_cache_hit", 0.0)
    return result


def _probe_view_definition(source_id: int, user_id: str, table_fqn: str, *, reason: str, session=None):
    """Request-scoped memoized wrapper around inspect_targeted_values's
    "view_definition" live probe for one VIEW candidate table -- same
    contract, rationale, and failure-caching discipline as
    _probe_view_row_count above (see its docstring); kept as a separate
    function only because the two probes have different cache-key
    namespaces and different investigation_type values, not because the
    caching policy differs. Shared by BOTH restricted-view call sites in
    this module (_discover_entity_contract_deterministic's bounded
    restriction check and gather_candidate_evidence's own per-candidate
    inspect loop) so a VIEW probed by either path within one request is
    never re-probed by the other.
    """
    from data.investigation_service import inspect_targeted_values

    def _compute():
        return inspect_targeted_values(
            source_id, user_id, table_fqn, None, investigation_type="view_definition", reason=reason,
        )

    if session is None:
        return _compute()

    from core.perf import stage_timer

    before_hits = session.request_context_hits
    result = session.get_or_compute(f"view_definition_probe:{source_id}:{table_fqn}", _compute)
    if session.request_context_hits > before_hits:
        stage_timer.record("view_definition_probe_cache_hit", 0.0)
    return result


def _get_persisted_view_semantics(source_id: int, table_fqn: str, *, session=None) -> dict | None:
    """View Semantic Discovery milestone — connect-time-persisted
    alternative to the live _probe_view_definition probe above, read from
    data.view_semantics_service's view_semantic_evidence table. Returns
    None when no row exists yet (e.g. the view was created after the last
    discovery run, or discovery hasn't reached this source yet), in which
    case the caller falls back to the live probe unchanged. Never raises."""
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        row = conn.execute(
            "SELECT definition_sql, referenced_tables_json, restriction_json "
            "FROM view_semantic_evidence WHERE source_id = ? AND table_fqn = ?",
            (source_id, table_fqn),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own_connection:
            conn.close()
    if row is None or not row["definition_sql"]:
        return None
    try:
        referenced_tables = json.loads(row["referenced_tables_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        referenced_tables = []
    try:
        restriction = json.loads(row["restriction_json"]) if row["restriction_json"] else None
    except (json.JSONDecodeError, TypeError):
        restriction = None
    return {
        "definition_sql": row["definition_sql"],
        "referenced_tables": referenced_tables,
        "restriction": restriction,
    }


def _verified_status_values_for_column(
    source_id: int, user_id: str, table_fqn: str, column_name: str, entity_name: str, *, session=None,
) -> tuple[list[str], str | None]:
    """Cached samples first, then at most ONE bounded live probe — shared by
    both _discover_entity_contract_deterministic's Step 5 and Task 2's
    gather_candidate_evidence, so both paths verify status values through
    the exact same bounded mechanism rather than two copies drifting apart.
    Returns (verified_values, value_source) where value_source is
    "cached"/"live_probe"/None.

    session, when given, memoizes this function's own return value per
    (source_id, table_fqn, column_name) for the life of the request --
    entity_name is deliberately NOT part of the cache key (it only affects
    the human-readable audit reason string passed to inspect_targeted_
    values's live-probe branch below, never the verified values themselves)
    -- gather_candidate_evidence's per-candidate inspection loop can ask
    for the SAME table's status column more than once in one question (an
    entity resolution pass and a later dimension/measure resolution pass
    both landing on the same candidate table), and a live probe is a real
    round trip against the source database, not just the internal metadata
    catalog.
    """
    def _compute() -> tuple[list[str], str | None]:
        verified_values = _sampled_status_values(source_id, table_fqn, column_name, session=session)
        if verified_values:
            return verified_values, "cached"
        from core.perf import stage_timer
        try:
            with stage_timer.measure("status_value_live_probe"):
                from data.investigation_service import inspect_targeted_values
                probe = inspect_targeted_values(
                    source_id, user_id, table_fqn, column_name,
                    investigation_type="distinct_values",
                    reason=f"Semantic grounding: verifying status values for {entity_name}.",
                )
            if probe.valid and probe.sample_values:
                return [str(v) for v in probe.sample_values if v is not None], "live_probe"
        except Exception:  # noqa: BLE001 — discovery must never fail the whole contract on a probe error
            logger.debug("semantic_contract_service: live status probe failed", exc_info=True)
        return [], None

    if session is None:
        return _compute()
    return session.get_or_compute(f"verified_status_values:{source_id}:{table_fqn}:{column_name}", _compute)


def _empty_contract_document(entity_name: str) -> dict:
    return {
        "entity_name": entity_name,
        "synonyms": list(entity_synonyms(entity_name)),
        "source_system_label": None,
        "grain": None,
        "primary_key": None,
        "attributes": [],
        "measures": [],
        "status_semantics": None,
        "date_semantics": {},
        "relationships": [],
        "excluded_objects": [],
        "alternate_candidates": [],
        "evidence": [],
    }


# ---------------------------------------------------------------------------
# Day 2C, Task 1 — Contract Validation Rules.
#
# One deterministic acceptance gate for ANY candidate object before it may
# become a persisted contract's canonical_table_fqn — whether the candidate
# came from discover_entity_contract's own _resolve_concept scoring above or
# from Task 2's AI-adjudication step. Every check reads evidence the caller
# already has in hand: `candidate` is one _resolve_concept candidate dict
# (table_fqn/name_score/ranking_reasons/relationships_summary — see that
# function's own return shape), `ctx` is the same business-context dict
# discover_entity_contract already hydrates via
# get_table_business_contexts_batch. This function never queries the
# database itself and never re-implements scoring — it only judges whether
# already-computed evidence clears a bar.
# ---------------------------------------------------------------------------

# Entities whose taxonomy meaning inherently implies a relationship to
# another entity (an Enrollment without a Student/Course link, a Placement
# without a Candidate/Job Order link, etc. cannot be the right table no
# matter how well its name scores) — zero discovered relationships is a
# hard rejection only for these; every other entity gets a non-blocking
# warning instead, since a legitimately standalone table (e.g. Invoice) can
# have no discovered relationships and still be correct.
_RELATIONSHIP_REQUIRED_ENTITIES = frozenset({
    "Enrollment", "Placement", "Job Order", "Recruiter", "Pilot Participant",
})

_MIN_UNIQUENESS_FOR_GRAIN = 0.95


@dataclass(frozen=True)
class ContractValidationResult:
    accepted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


def validate_candidate_contract(
    entity_name: str,
    candidate: dict,
    ctx: dict | None,
    *,
    requested_attributes: list[str] | None = None,
    claimed_status_column: str | None = None,
    claimed_date_columns: list[str] | None = None,
    value_samples: list[str] | None = None,
    intentional_analytical: bool = False,
    additional_valid_columns: "set[str] | None" = None,
) -> ContractValidationResult:
    """Accept/reject one candidate object as entity_name's canonical table.

    `candidate` — one candidate dict as returned by
    data.query_planning_service._resolve_concept (table_fqn, name_score,
    ranking_reasons, relationships_summary, ...).
    `ctx` — the matching business-context dict for candidate["table_fqn"]
    (get_table_business_contexts_batch's per-table shape: table/dictionary/
    profiling/columns/relationships/...), or None if no metadata context
    exists for it at all.
    `requested_attributes` — business field names the question/adjudication
    step asked for; every one must map to a real column to accept.
    `claimed_status_column`/`claimed_date_columns` — column names a caller
    is about to persist as status/date semantics; each must exist on this
    table's own columns, OR on `additional_valid_columns` (a trusted
    secondary object's columns — e.g. a preferred analytical view that
    legitimately carries the status column instead of the canonical table
    itself, a real shape found revalidating Pilot Participant: a base
    membership table plus a separate current-status view).
    `value_samples` — verified sample values already collected for
    claimed_status_column, if any (a warning, not a rejection, when absent —
    see the module's PARTIAL/RESOLVED distinction).
    `intentional_analytical` — set by a caller that is deliberately
    selecting a derivative/rollup object as an analytical view, bypassing
    the derivative-object rejection only.
    """
    table_fqn = candidate.get("table_fqn") or ((ctx or {}).get("table") or {}).get("table_fqn") or "<unknown>"
    reasons: list[str] = []
    warnings: list[str] = []

    # 1. canonical object exists — real profiling or dictionary evidence,
    # not just a name a caller (e.g. the AI adjudicator) typed.
    if ctx is None:
        return ContractValidationResult(
            False, reasons=(f"'{table_fqn}' has no metadata context at all — cannot validate.",),
        )
    if not ctx.get("profiling") and not ctx.get("dictionary"):
        reasons.append(f"'{table_fqn}' has no profiling or dictionary evidence — object existence is unverified.")

    columns = ctx.get("columns") or []
    column_names = {c["column_name"] for c in columns}

    # 2/3. grain can be described + identity key exists or is evidenced.
    has_identity_key = any(
        (c.get("schema") or {}).get("is_primary_key") or (c.get("schema") or {}).get("is_identity")
        for c in columns
    )
    has_uniqueness_evidence = any(
        ((c.get("profiling") or {}).get("uniqueness_score") or 0) >= _MIN_UNIQUENESS_FOR_GRAIN for c in columns
    )
    has_documented_grain = bool((ctx.get("dictionary") or {}).get("grain"))
    if not (has_identity_key or has_uniqueness_evidence or has_documented_grain):
        reasons.append(
            f"'{table_fqn}' has no primary/identity key or uniqueness evidence — grain cannot be described.",
        )
    elif not has_identity_key and not has_documented_grain:
        warnings.append(f"'{table_fqn}' grain is inferred from column uniqueness only; no declared identity key.")

    # 4. important requested attributes exist.
    missing_attrs = [
        attr for attr in (requested_attributes or [])
        if not any(
            attr.lower() in (c["column_name"] or "").lower()
            or attr.lower() in ((c.get("dictionary") or {}).get("business_label") or "").lower()
            for c in columns
        )
    ]
    if missing_attrs:
        reasons.append(f"Requested attribute(s) not found on '{table_fqn}': {', '.join(missing_attrs)}.")

    # 5. entity name has meaningful lexical/business evidence — rejects a
    # candidate that only matches a generic word (name_score<=0) with
    # nothing else backing it, which also covers "confidence depends only
    # on generated vocabulary" (a generated-vocab bonus never raises
    # name_score itself).
    name_score = candidate.get("name_score", 0.0) or 0.0
    has_business_name = bool((ctx.get("dictionary") or {}).get("business_name"))
    if name_score <= 0 and not has_business_name:
        reasons.append(
            f"'{table_fqn}' matches only a generic word, with no business-name/description "
            f"evidence tying it to '{entity_name}'.",
        )

    # 6. relationships match the proposed business role. Mirrors the same
    # two-tier trust the reference database's own join-selection/scoring already use
    # (query_planning_service._score_relationship_centrality,
    # semantic_layer_service._load_edges): only AUTO/APPROVED edges
    # (ctx["relationships"]) are treated as verified; a PENDING-only edge
    # (ctx["relationship_evidence"], unfiltered by status) is real signal
    # that hasn't cleared governance yet — a warning, not proof of nothing.
    # Hard rejection is reserved for genuinely zero relationship evidence at
    # ANY status, which the reference database's real Pilot Participant data showed is a
    # meaningfully different (worse) case than "PENDING but unapproved".
    relationships = ctx.get("relationships") or {}
    has_relationship = bool(relationships.get("outbound") or relationships.get("inbound"))
    has_pending_relationship = any(
        (r.get("relationship_status") == "PENDING") for r in (ctx.get("relationship_evidence") or [])
    )
    if not has_relationship:
        if has_pending_relationship:
            warnings.append(
                f"'{table_fqn}' has only unapproved (PENDING) relationship evidence — no AUTO/APPROVED "
                "relationship path yet.",
            )
        elif entity_name in _RELATIONSHIP_REQUIRED_ENTITIES:
            reasons.append(
                f"'{table_fqn}' has no discovered relationships (at any status), but '{entity_name}' "
                "requires a relationship path to be trustworthy as canonical.",
            )
        else:
            warnings.append(f"'{table_fqn}' has no discovered relationships.")

    # 7. derivative/export/history/aggregate objects are not selected as
    # canonical unless intentionally marked analytical — reuses the SAME
    # naming-penalty flag discover_entity_contract's own exclusion loop
    # already relies on, never a second classifier.
    penalty_reason = next(
        (r for r in (candidate.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
    )
    if penalty_reason and not intentional_analytical:
        reasons.append(f"'{table_fqn}' was flagged as a derivative object ({penalty_reason}).")

    # 8. status/date semantics reference real columns (on this table or the
    # trusted additional_valid_columns object, e.g. a preferred view).
    status_date_column_names = column_names | (additional_valid_columns or set())
    if claimed_status_column and claimed_status_column not in status_date_column_names:
        reasons.append(f"Claimed status column '{claimed_status_column}' does not exist on '{table_fqn}'.")
    for date_col in (claimed_date_columns or []):
        if date_col not in status_date_column_names:
            reasons.append(f"Claimed date column '{date_col}' does not exist on '{table_fqn}'.")

    # 9. sampled values are verified when needed — non-blocking: an
    # unverified status column makes the eventual contract PARTIAL rather
    # than RESOLVED (discover_entity_contract's own existing distinction),
    # not grounds to reject the canonical object itself.
    if claimed_status_column and not value_samples:
        warnings.append(f"Status column '{claimed_status_column}' on '{table_fqn}' has no verified sample values yet.")

    return ContractValidationResult(accepted=not reasons, reasons=tuple(reasons), warnings=tuple(warnings))


def _discover_entity_contract_deterministic(
    source_id: int, user_id: str, entity_name: str, *, session=None,
) -> dict:
    """Day 2B Task 1 — one bounded, purely deterministic discovery pass for
    one target entity. Returns a dict shaped for persist_entity_contract's
    keyword arguments: {canonical_table_fqn, preferred_view_fqn,
    resolution_status, confidence, contract_json, metadata_revision}. Never
    inspects the database beyond: one bounded candidate search + one
    batched context hydration of the candidates it returned + at most one
    bounded live value-probe.

    Day 2C — this is now the FIRST pass only; see discover_entity_contract
    below, which falls back to AI-assisted candidate adjudication when this
    function's own scoring lands on NO_CANDIDATE.
    """
    from data.query_planning_service import (
        _AMBIGUITY_MARGIN,
        _AUTO_SELECT_MIN_CONFIDENCE,
        _find_status_column,
        _is_dimension_column,
        _is_metric_column,
        _resolve_concept,
        _select_entity_key,
    )
    from data.semantic_retrieval_service import get_candidate_tables_with_ranking
    from data.business_knowledge_service import get_table_business_contexts_batch
    from data.knowledge_graph_service import get_related_tables
    from data.vocabulary_bootstrap_service import get_generated_vocabulary
    from data.concept_mapping_service import get_all_approved_mappings

    metadata_revision = current_metadata_revision(source_id, session=session)
    synonyms = entity_synonyms(entity_name)
    synthetic_question = " ".join(synonyms)

    # Step 1 — lightweight retrieval (the same bounded, ~25-table-capped
    # search the query-time planner already does per question).
    candidate_tables, _ranking, _remembered = get_candidate_tables_with_ranking(
        source_id, user_id, synthetic_question, list(synonyms), session=session,
    )
    if not candidate_tables:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append("Bounded candidate retrieval returned no tables for any synonym.")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    # Step 2 — inspect only the top relevant objects the search returned.
    table_contexts = get_table_business_contexts_batch(source_id, user_id, candidate_tables, session=session)
    table_contexts = {fqn: ctx for fqn, ctx in table_contexts.items() if ctx}

    # Step 3 — rank via the SAME primitive query-time resolution uses
    # (already hardened by the Day 2A trust/specificity guard), once per
    # synonym, merged by keeping each table's best score across synonyms.
    # concept_mappings/generated_vocab mirror query_planning_service.py's
    # own construction for the live path (previously omitted here, so a
    # table's generated-vocabulary evidence — e.g. a matching column name —
    # survived retrieval but was never actually counted toward its score).
    concept_mappings = get_all_approved_mappings(source_id, session=session)
    generated_vocab = get_generated_vocabulary(source_id, session=session)

    merged: dict[str, dict] = {}
    for term in synonyms:
        result = _resolve_concept(term, table_contexts, concept_mappings, generated_vocab)
        for cand in result["candidates"]:
            fqn = cand["table_fqn"]
            if fqn not in merged or cand["score"] > merged[fqn]["score"]:
                merged[fqn] = cand
    ranked = sorted(merged.values(), key=lambda c: -c["score"])

    if not ranked:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append("Candidates were found but none scored against any synonym.")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    # Step 4 — excluded derivative objects: anything the existing naming-
    # penalty scorer already flagged (backup/export/history/rolling/dated
    # copy), recorded rather than silently dropped, and never eligible for
    # canonical/preferred-view selection below.
    excluded_objects = []
    eligible = []
    for cand in ranked:
        penalty_reason = next(
            (r for r in (cand.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
        )
        if penalty_reason:
            excluded_objects.append({"table_fqn": cand["table_fqn"], "reason": penalty_reason})
        else:
            eligible.append(cand)

    if not eligible:
        doc = _empty_contract_document(entity_name)
        doc["excluded_objects"] = excluded_objects
        doc["evidence"].append("Every candidate was a flagged derivative object (backup/export/history/...).")
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    # 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-view
    # safety, deterministic path. This function (not the AI-adjudication
    # fallback) is the one that actually wins whenever _resolve_concept
    # already scores a non-empty top candidate — live-confirmed a VIEW
    # whose own definition contains a hardcoded WHERE literal (e.g.
    # "WHERE TrackID IN (1000)") can lexically outscore the true, broader
    # base table for an unqualified entity request and become canonical
    # here without ever reaching the AI adjudicator at all. A restricted
    # VIEW must not become canonical_table_fqn; if its own FROM-clause
    # names a real, broader table in this source's catalog (structural
    # lineage), that table takes its place in `eligible` at the same
    # confidence; otherwise the view is simply excluded and the next
    # eligible candidate (or NO_CANDIDATE, below) governs — never a
    # silent partial-population answer.
    probes_used = 0
    i = 0
    while i < len(eligible) and probes_used < _MAX_RESTRICTION_PROBES:
        cand = eligible[i]
        fqn = cand["table_fqn"]
        ctx = table_contexts.get(fqn)
        if not ctx or (ctx.get("table") or {}).get("table_type") != "VIEW":
            i += 1
            continue
        probes_used += 1
        vd = _probe_view_definition(
            source_id, user_id, fqn,
            reason=f"Semantic grounding: checking {fqn} for restriction before canonical selection.",
            session=session,
        )
        definition_text = vd.sample_values[0] if (vd.valid and vd.sample_values) else None
        restriction = _detect_view_restriction(definition_text)
        if not restriction or not restriction["restriction_detected"]:
            i += 1
            continue
        eligible.pop(i)
        excluded_objects.append({
            "table_fqn": fqn,
            "reason": (
                "Restricted view: definition indicates a filtered business slice "
                f"({', '.join(restriction['restriction_kinds'])}), not the full population."
            ),
        })
        already_present = {c["table_fqn"] for c in eligible} | {d["table_fqn"] for d in excluded_objects}
        root = _extract_lineage_root_table(definition_text)
        if root and root not in already_present:
            root_ctx = get_table_business_contexts_batch(source_id, user_id, [root], session=session).get(root)
            if root_ctx:
                table_contexts[root] = root_ctx
                eligible.insert(i, {
                    "table_fqn": root, "score": cand["score"], "name_score": cand.get("name_score") or 0.0,
                    "ranking_reasons": [
                        f"Structural lineage: {fqn}'s own FROM clause is built on this table.",
                    ],
                })
                continue  # re-examine the newly inserted candidate at this same index
        # no usable lineage root: don't advance i — the list shrank by one,
        # so the next candidate is already at this same index.

    if not eligible:
        doc = _empty_contract_document(entity_name)
        doc["excluded_objects"] = excluded_objects
        doc["evidence"].append(
            "Every remaining candidate was a restricted view with no resolvable broader source — "
            "refusing rather than returning a silent partial-population answer."
        )
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": 0.0,
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    top = eligible[0]
    runner_up_score = eligible[1]["score"] if len(eligible) > 1 else 0.0
    table_confident = (
        top["score"] >= _AUTO_SELECT_MIN_CONFIDENCE and (top["score"] - runner_up_score) >= _AMBIGUITY_MARGIN
    )
    if not table_confident:
        doc = _empty_contract_document(entity_name)
        doc["excluded_objects"] = excluded_objects
        doc["evidence"].append(
            f"Top candidate '{top['table_fqn']}' (score {top['score']:.2f}) did not clear the "
            f"auto-select confidence/margin gate against '{eligible[1]['table_fqn']}' "
            f"(score {runner_up_score:.2f})." if len(eligible) > 1 else
            f"Top candidate '{top['table_fqn']}' (score {top['score']:.2f}) is below the "
            f"auto-select confidence threshold ({_AUTO_SELECT_MIN_CONFIDENCE:.2f})."
        )
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_CANDIDATE", "confidence": top["score"],
            "contract_json": doc, "metadata_revision": metadata_revision,
        }

    canonical_table_fqn = top["table_fqn"]
    canonical_ctx = table_contexts[canonical_table_fqn]
    canonical_is_view = (canonical_ctx.get("table") or {}).get("table_type") == "VIEW"

    # A SMALL, bounded set of next-best eligible candidates — not full
    # broad-search candidates, but enough for Day 2A's Task 4 bounded
    # status-retry (_resolve_status_compatible_entity) to still have a real
    # alternative to retry against when the canonical table itself has no
    # compatible status field, instead of the query-time candidate pool
    # being narrowed to the single canonical table and silently defeating
    # that mechanism.
    alternate_candidates = [c["table_fqn"] for c in eligible[1:1 + _MAX_ALTERNATE_CANDIDATES]]

    preferred_view_fqn = canonical_table_fqn if canonical_is_view else None
    if preferred_view_fqn is None:
        best_view = next(
            (c for c in eligible[1:]
             if (table_contexts.get(c["table_fqn"], {}).get("table") or {}).get("table_type") == "VIEW"
             and c["score"] >= _AUTO_SELECT_MIN_CONFIDENCE),
            None,
        )
        if best_view:
            preferred_view_fqn = best_view["table_fqn"]

    # Step 5 — status semantics: column via the existing finder, verified
    # values via cached samples first, then at most ONE bounded live probe.
    status_semantics = None
    status_col = _find_status_column({canonical_table_fqn: canonical_ctx}, canonical_table_fqn)
    if status_col:
        _, status_column_name = status_col
        col_ctx = next(
            (c for c in canonical_ctx.get("columns", []) if c["column_name"] == status_column_name), None,
        )
        data_type = ((col_ctx or {}).get("schema") or {}).get("data_type")
        verified_values, value_source = _verified_status_values_for_column(
            source_id, user_id, canonical_table_fqn, status_column_name, entity_name, session=session,
        )
        status_semantics = {
            "table_fqn": canonical_table_fqn, "column_name": status_column_name,
            "data_type": data_type, "verified_values": verified_values,
            "value_source": value_source,
        }

    # Step 6 — date semantics, measures, attributes, primary key: existing
    # classifiers applied to the canonical table's own already-loaded columns.
    date_semantics = _classify_date_columns(canonical_ctx)
    measures = [
        {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
         "column_name": c["column_name"]}
        for c in canonical_ctx.get("columns", []) if _is_metric_column(c)
    ]
    attributes = [
        {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
         "column_name": c["column_name"]}
        for c in canonical_ctx.get("columns", []) if _is_dimension_column(c)
    ]
    primary_key = _select_entity_key(canonical_ctx)

    # Step 7 — trusted relationships: already-trusted 1-hop neighborhood,
    # raw evidence recorded here; query-time Task 5 integration cross-
    # references neighbor table_fqns against OTHER matched entities'
    # contracts, rather than this discovery pass guessing entity identity
    # for tables it isn't grounding right now.
    related = get_related_tables(source_id, user_id, canonical_table_fqn) or {}
    relationships = [
        {"table_fqn": r["table_fqn"], "relationship_types": r.get("relationship_types"),
         "confidence": r.get("confidence")}
        for r in (related.get("related_tables") or [])
    ]

    source_row = canonical_ctx.get("source") or {}
    dict_row = canonical_ctx.get("dictionary") or {}
    # RESOLVED by default (including "no status concept on this entity at
    # all" — that's a fine, complete answer, not a gap). PARTIAL only when
    # a status COLUMN was found but no verified values could be attached to
    # it (neither cached samples nor the one bounded live probe succeeded).
    resolution_status = "PARTIAL" if status_col and not status_semantics["verified_values"] else "RESOLVED"

    contract_json = {
        "entity_name": entity_name,
        "synonyms": list(synonyms),
        "source_system_label": source_row.get("display_name") or source_row.get("source_type"),
        "grain": dict_row.get("grain") or f"One row per {dict_row.get('business_name') or entity_name}.",
        "primary_key": (
            {"table_fqn": canonical_table_fqn, **primary_key} if primary_key else None
        ),
        "attributes": attributes,
        "measures": measures,
        "status_semantics": status_semantics,
        "date_semantics": date_semantics,
        "relationships": relationships,
        "excluded_objects": excluded_objects,
        "alternate_candidates": alternate_candidates,
        "evidence": [
            f"Resolved via synonyms {list(synonyms)} against {len(candidate_tables)} bounded candidate(s).",
            f"Top score {top['score']:.3f} vs runner-up {runner_up_score:.3f}.",
        ],
    }

    return {
        "canonical_table_fqn": canonical_table_fqn, "preferred_view_fqn": preferred_view_fqn,
        "resolution_status": resolution_status, "confidence": top["score"],
        "contract_json": contract_json, "metadata_revision": metadata_revision,
    }


def _promote_column_evidence_candidates(
    term: str, table_contexts: dict[str, dict], merged: dict[str, dict],
    concept_mappings: dict, generated_vocab: dict,
) -> set[str]:
    """AI Schema Retrieval Gap fix, role-aware column-evidence promotion
    (Parts 2-4): a business concept (measure/attribute) can be represented
    primarily by a COLUMN even when its owning table has little/no lexical
    similarity to the term. _resolve_concept's own candidates[:5] per-term
    slice never sees such a table when a handful of unrelated table-NAME
    matches happen to outscore it lexically.

    Mutates `merged` in place, additively only: never overwrites a table
    _resolve_concept already placed there (that candidate is already the
    real, fully-scored one). For any OTHER table with genuine column-level
    concept-mapping/generated-vocabulary evidence for `term`, computes that
    table's real candidate via the exact same _build_concept_candidate
    scoring _resolve_concept itself uses -- nothing invented, no new
    scoring mechanism. Bounded to the strongest
    _MAX_COLUMN_EVIDENCE_PROMOTIONS_PER_TERM column-evidenced tables per
    term so this stays deterministic and small, mirroring
    data/semantic_retrieval_service.py's own reserved column-evidence lane.

    Returns the set of table_fqns actually added (empty if none) -- the
    caller uses this as a reserved-lane guarantee so a promoted table also
    survives gather_candidate_evidence's own top-N "inspected" cut, not
    just the wider pool cut (a promoted table's raw score can still be
    genuinely lower than an unrelated table-name match, e.g. a backup/
    duplicate table that merely shares a name token -- the promotion's
    whole point is that it must not be invisible to the adjudicator purely
    because of that, same as it must not be invisible to the pool).

    AI-path only (gather_candidate_evidence's own caller). _resolve_concept
    and every legacy caller that invokes it directly are untouched."""
    from data.query_planning_service import (
        _best_column_evidence, _build_concept_candidate, _concept_mapping_lookup, _generated_vocab_lookup,
    )

    mapping_lookup = _concept_mapping_lookup(concept_mappings, term)
    vocab_lookup = _generated_vocab_lookup(generated_vocab, term)
    if not mapping_lookup and not vocab_lookup:
        return set()

    column_evidenced_fqns = {
        fqn for (fqn, col) in list(mapping_lookup) + list(vocab_lookup)
        if col and fqn not in merged and fqn in table_contexts
    }
    if not column_evidenced_fqns:
        return set()

    def _evidence_score(fqn: str) -> float:
        best = 0.0
        for lookup in (mapping_lookup, vocab_lookup):
            row = _best_column_evidence(lookup, fqn)
            if row is None:
                continue
            score = row.get("confidence")
            if score is None:
                score = row.get("confidence_score") or 0.0
            best = max(best, score)
        return best

    ranked_fqns = sorted(column_evidenced_fqns, key=lambda fqn: -_evidence_score(fqn))
    promoted = ranked_fqns[:_MAX_COLUMN_EVIDENCE_PROMOTIONS_PER_TERM]
    for fqn in promoted:
        merged[fqn] = _build_concept_candidate(
            term, fqn, table_contexts[fqn], mapping_lookup, vocab_lookup, None,
        )
    return set(promoted)


# ---------------------------------------------------------------------------
# Day 2C, Task 2 — AI-Assisted Candidate Adjudication.
#
# A fallback path only: runs exclusively when
# _discover_entity_contract_deterministic (Day 2B's own scoring, unchanged
# above) already landed on NO_CANDIDATE. Composes the SAME retrieval/
# context/relationship/status-verification primitives that function uses —
# no new resolution algorithm, no new database access pattern. The AI step
# itself lives in core.semantic.candidate_adjudicator, follows the exact
# fail-closed provider idiom already established by
# core.semantic.ai_interpreter (disabled by default, any error/timeout/
# schema violation returns None), and never selects anything outside the
# evidence handed to it. Every AI selection is re-validated by Task 1's
# validate_candidate_contract before it may become a persisted contract.
# ---------------------------------------------------------------------------

def gather_candidate_evidence(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Bounded evidence-gathering pass for one entity — see
    _gather_candidate_evidence_uncached for the full contract/return shape.

    PERFORMANCE PHASE A — request-scoped memoization. This function's
    output is a pure function of its three arguments (source_id, user_id,
    entity_name); it takes no other input. Live-confirmed forensic finding:
    the SAME (source_id, user_id, entity_name) call is made repeatedly
    within one request -- once per ROLE a term plays (an entity resolution
    pass, then a later dimension/measure resolution pass naming the exact
    same term, e.g. "program" named as both the entity AND the grouping
    dimension for REG-G-03-shaped questions -- see core.semantic.
    candidate_adjudicator's own docstring, which already documents this
    exact repeat-call shape for its own, narrower adjudication cache) --
    averaging 3.84 calls/request and up to 20 for a single question,
    accounting for 64.7% of total wall time across the 37-question suite.

    When session is given (data.request_metadata_session.RequestMetadataSession),
    this is memoized per request using the SAME session.get_or_compute
    primitive and the SAME "cache a legitimate empty/no-candidate result,
    never re-decide selection logic" discipline already established by
    _verified_status_values_for_column below and by core.semantic.
    candidate_adjudicator.adjudicate_candidates's own session-scoped cache
    -- not a new caching mechanism, the same one applied one layer higher.
    A cache HIT returns the byte-identical dict object a fresh call would
    have computed (same code path, same DB state within one request) --
    it never reinterprets, filters, or re-ranks anything; it only skips
    redundant recomputation of an operation that would deterministically
    reproduce the same output. Without a session, behaves exactly as
    before (a legacy caller not yet threaded through a session pays the
    full, uncached cost every time, same as before this change).
    """
    if session is None:
        return _gather_candidate_evidence_uncached(source_id, user_id, entity_name, session=session)

    from core.perf import stage_timer

    before_hits = session.request_context_hits
    result = session.get_or_compute(
        f"gather_candidate_evidence:{source_id}:{entity_name}",
        lambda: _gather_candidate_evidence_uncached(source_id, user_id, entity_name, session=session),
    )
    if session.request_context_hits > before_hits:
        stage_timer.record("gather_candidate_evidence_cache_hit", 0.0)
    return result


def _gather_candidate_evidence_uncached(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """The actual discovery pass -- see gather_candidate_evidence (the
    memoized public entry point) for the request-scoped caching wrapper.
    Never call this directly from outside this module; always go through
    gather_candidate_evidence so the cache stays the single source of
    truth for "has this exact (source_id, entity_name) already been
    resolved this request."

    Returns:
      {"entity_name":, "synonyms":,
       "pool_table_fqns": [... up to 10, score-ordered ...],
       "evidence": [... up to 5 compact per-candidate dicts, AI-prompt-ready ...],
       "candidates_by_fqn": {table_fqn: {"candidate": <_resolve_concept candidate dict>,
                                          "ctx": <business-context dict>}, ...}}  # the inspected ones
    """
    from data.query_planning_service import (
        _find_status_column, _is_dimension_column, _is_metric_column, _resolve_concept, _select_entity_key,
        _is_genuine_label_column, _is_person_reference_column,
    )
    from data.semantic_retrieval_service import (
        get_candidate_tables_with_ranking, PRELIMINARY_BUSINESS_LEARNING_PROVENANCE,
        CURATED_VIEW_EVIDENCE_PROVENANCE,
    )
    from data.business_knowledge_service import get_table_business_contexts_batch
    from data.knowledge_graph_service import get_related_tables
    from data.vocabulary_bootstrap_service import get_generated_vocabulary
    from data.concept_mapping_service import get_all_approved_mappings
    from core.perf import stage_timer
    import time as _time

    _gce_t0 = _time.monotonic()

    synonyms = entity_synonyms(entity_name)
    synthetic_question = " ".join(synonyms)
    empty = {
        "entity_name": entity_name, "synonyms": list(synonyms),
        "pool_table_fqns": [], "evidence": [], "candidates_by_fqn": {},
    }

    candidate_tables, _retrieval_ranking, _remembered = get_candidate_tables_with_ranking(
        source_id, user_id, synthetic_question, list(synonyms), session=session,
    )
    if not candidate_tables:
        stage_timer.record("gather_candidate_evidence", (_time.monotonic() - _gce_t0) * 1000)
        return empty

    # Business Learning Candidate Discovery milestone, Part 5 — provenance
    # survives retrieval. A table whose ONLY discovery signal in this call's
    # retrieval was the preliminary-business-learning source (see
    # data.semantic_retrieval_service.get_preliminary_candidates) keeps that
    # tag all the way into the per-candidate evidence bundle
    # core.semantic.candidate_adjudicator actually sees (candidate_provenance
    # below) — so the adjudicator can tell "merely suggested" apart from
    # "found by real search/vocabulary/mapping evidence" instead of the two
    # being indistinguishable once both reach the same candidate pool. A
    # table ALSO found by a stronger source lost this tag already, upstream,
    # in _retrieve's own score-ordered merge (validated evidence always
    # outranks and overwrites the preliminary floor there) — so this map
    # only ever names candidates preliminary discovery alone surfaced.
    preliminary_provenance_by_fqn = {
        r["qualified_name"]: r.get("provenance_reason")
        for r in _retrieval_ranking
        if r.get("provenance") == PRELIMINARY_BUSINESS_LEARNING_PROVENANCE
    }
    # View Semantic Discovery milestone — identical contract, one rung up
    # the same floor ladder (see CURATED_VIEW_EVIDENCE_PROVENANCE's own
    # docstring): a table found SOLELY via a curated view's own definition/
    # dependency evidence keeps that tag into candidate_provenance below.
    # Merged with the preliminary map since a table can only ever carry one
    # provenance tag (whichever source's dict won _retrieve's score-ordered
    # merge) — the two source sets are disjoint by construction.
    curated_view_provenance_by_fqn = {
        r["qualified_name"]: r.get("provenance_reason")
        for r in _retrieval_ranking
        if r.get("provenance") == CURATED_VIEW_EVIDENCE_PROVENANCE
    }
    preliminary_provenance_by_fqn.update(curated_view_provenance_by_fqn)

    table_contexts = get_table_business_contexts_batch(source_id, user_id, candidate_tables, session=session)
    table_contexts = {fqn: ctx for fqn, ctx in table_contexts.items() if ctx}
    if not table_contexts:
        stage_timer.record("gather_candidate_evidence", (_time.monotonic() - _gce_t0) * 1000)
        return empty

    # Score with the SAME evidence the live query-planning path scores with
    # (query_planning_service.py:4258's own generated_vocab/concept_mappings
    # construction, mirrored here) — previously omitted, which meant a
    # table already surviving retrieval on generated-vocabulary evidence
    # (e.g. a matching column name) got scored here with none of that
    # evidence counted, only its bare lexical/authority signal. Bulk reads,
    # once per call, not once per term/candidate.
    concept_mappings = get_all_approved_mappings(source_id, session=session)
    generated_vocab = get_generated_vocabulary(source_id, session=session)

    merged: dict[str, dict] = {}
    promoted_fqns: set[str] = set()
    for term in synonyms:
        result = _resolve_concept(term, table_contexts, concept_mappings, generated_vocab)
        for cand in result["candidates"]:
            fqn = cand["table_fqn"]
            if fqn not in merged or cand["score"] > merged[fqn]["score"]:
                merged[fqn] = cand
        promoted_fqns |= _promote_column_evidence_candidates(
            term, table_contexts, merged, concept_mappings, generated_vocab,
        )
    ranked = sorted(merged.values(), key=lambda c: -c["score"])[:_MAX_ADJUDICATION_POOL]
    pool_table_fqns = [c["table_fqn"] for c in ranked]
    # LIVE PROGRAM ADJUDICATION MILESTONE, Part 1/4 — deterministic rank
    # position (1-indexed, by score) surfaced to the adjudicator below.
    # Live-confirmed the prior evidence shape gave the LLM no explicit
    # signal of which candidate the deterministic layer actually favored —
    # only a bare unlabeled "score" float with no ranking context and no
    # guidance in the system prompt to weight it.
    deterministic_rank_by_fqn = {c["table_fqn"]: i + 1 for i, c in enumerate(ranked)}

    # Role-aware column-evidence promotion, continued: a promoted table's
    # raw score can still trail an unrelated table-name match (that's
    # exactly the shape it exists to fix), so guarantee it also survives
    # the narrower "inspected" cut below rather than only the wider pool
    # cut -- otherwise it would be visible in pool_table_fqns but still
    # never reach the adjudicator. Still fully bounded: reserved entries
    # are a subset of `ranked` (itself capped at _MAX_ADJUDICATION_POOL),
    # so inspected size is at most _MAX_ADJUDICATION_POOL, never unbounded.
    to_inspect = ranked[:_MAX_ADJUDICATION_INSPECTED]
    inspected_fqns = {c["table_fqn"] for c in to_inspect}
    for cand in ranked[_MAX_ADJUDICATION_INSPECTED:]:
        if cand["table_fqn"] in promoted_fqns and cand["table_fqn"] not in inspected_fqns:
            to_inspect.append(cand)
            inspected_fqns.add(cand["table_fqn"])

    evidence: list[dict] = []
    candidates_by_fqn: dict[str, dict] = {}
    lineage_root_fqns_added: set[str] = set()
    _inspect_loop_t0 = _time.monotonic()

    def _build_evidence_entry(fqn: str, cand: dict, ctx: dict, *, lineage_source_view: str | None = None) -> dict:
        candidates_by_fqn[fqn] = {"candidate": cand, "ctx": ctx}
        columns = ctx.get("columns") or []

        status_evidence = None
        status_col = _find_status_column({fqn: ctx}, fqn)
        if status_col:
            _, status_column_name = status_col
            verified_values, value_source = _verified_status_values_for_column(
                source_id, user_id, fqn, status_column_name, entity_name, session=session,
            )
            status_evidence = {
                "column_name": status_column_name,
                "verified_values": verified_values[:_MAX_SAMPLE_VALUES],
                "value_source": value_source,
            }

        related = get_related_tables(source_id, user_id, fqn) or {}
        penalty_reason = next(
            (r for r in (cand.get("ranking_reasons") or []) if r.startswith("Naming penalty:")), None,
        )

        # LIVE PROGRAM ADJUDICATION MILESTONE, Part 3/4 — normalized,
        # LLM-legible business-role evidence. Live-confirmed root cause of
        # the LLM overriding the (correctly higher-ranked) catalog
        # candidate: the evidence dict carried a bare "score" float and an
        # auto-generated one-line description ("Contains general data...")
        # with no explicit signal of WHY a table is/isn't a browsable named
        # entity — the same structural signals data.query_planning_service.
        # _score_table_authority already computes (genuine label columns,
        # person-reference columns) were never surfaced here at all. Reuses
        # those exact same generic, no-the reference database-table-names-hardcoded checks —
        # not a new/duplicate heuristic.
        descriptor_columns = [
            c["column_name"] for c in columns if _is_genuine_label_column(c)
        ][:_MAX_EVIDENCE_COLUMNS]
        person_ref_columns = [
            c["column_name"] for c in columns if _is_person_reference_column(c, fqn)
        ][:_MAX_EVIDENCE_COLUMNS]
        if descriptor_columns and not person_ref_columns:
            business_role = "catalog_or_entity"
        elif person_ref_columns and not descriptor_columns:
            business_role = "assignment_or_event"
        elif person_ref_columns and descriptor_columns:
            business_role = "assignment_or_event"  # a person-reference column dominates: still per-person grain
        else:
            business_role = "unknown"

        object_type = (ctx.get("table") or {}).get("table_type")
        approx_row_count = (
            (ctx.get("profiling") or {}).get("exact_row_count")
            or (ctx.get("profiling") or {}).get("estimated_row_count")
        )

        # 37-GATE CORRECTNESS REPAIR MILESTONE, Part 1/2 — restricted-view
        # safety. Profiling never covers VIEWs (approx_row_count is always
        # None for one here otherwise), and nothing previously told the
        # adjudicator whether a VIEW represents the full apparent population
        # or a filtered business slice. One bounded live probe each, only
        # for VIEW candidates actually reaching the adjudicator (already
        # capped at _MAX_ADJUDICATION_INSPECTED) — same per-candidate
        # live-probe budget status_value_live_probe already spends above.
        restricted_view_evidence = None
        referenced_tables_evidence: list[str] = []
        lineage_root_fqn: str | None = None
        if object_type == "VIEW":
            if approx_row_count is None:
                rc = _probe_view_row_count(
                    source_id, user_id, fqn,
                    reason=f"Semantic grounding: profiling row count for VIEW candidate {fqn}.",
                    session=session,
                )
                if rc.valid and rc.sample_values:
                    try:
                        approx_row_count = int(rc.sample_values[0])
                    except (TypeError, ValueError):
                        pass

            # View Semantic Discovery milestone — prefer the connect-time-
            # persisted definition (data.view_semantics_service) over a live
            # per-request probe when it's already available; same
            # restriction/lineage semantics either way, just skips a live
            # round-trip. Falls back to the original live probe, unchanged,
            # when no persisted row exists yet.
            persisted = _get_persisted_view_semantics(source_id, fqn, session=session)
            if persisted is not None:
                definition_text = persisted["definition_sql"]
                restriction = persisted["restriction"]
                referenced_tables_evidence = persisted["referenced_tables"]
            else:
                vd = _probe_view_definition(
                    source_id, user_id, fqn,
                    reason=f"Semantic grounding: checking {fqn} for restriction/lineage evidence.",
                    session=session,
                )
                definition_text = vd.sample_values[0] if (vd.valid and vd.sample_values) else None
                restriction = _detect_view_restriction(definition_text)
                referenced_tables_evidence = _extract_all_referenced_tables(definition_text)

            if restriction is not None:
                restricted_view_evidence = restriction
                if restriction["restriction_detected"] and lineage_source_view is None:
                    root = referenced_tables_evidence[0] if referenced_tables_evidence else (
                        _extract_lineage_root_table(definition_text)
                    )
                    # RESTRICTED-VIEW BACKSTOP LINEAGE FIX — recorded on THIS
                    # view's own entry unconditionally (not only when a
                    # synthetic candidate is actually added below), so
                    # _apply_restricted_view_backstop can still find the
                    # shared root when two restricted views reference the
                    # SAME underlying table: the first view to be processed
                    # claims the root via _try_add_lineage_candidate, and
                    # every later view referencing that same root skips
                    # re-adding it (root already in candidates_by_fqn) —
                    # without this field, that later view's own lineage
                    # association was previously lost entirely, and the
                    # backstop (which only matched a synthetic entry's own
                    # lineage_source_view tag against the SELECTED view) saw
                    # zero alternatives and rejected a resolvable selection.
                    if root and root != fqn:
                        lineage_root_fqn = root
                    if (
                        root and root != fqn and root not in candidates_by_fqn
                        and root not in lineage_root_fqns_added
                        and len(lineage_root_fqns_added) < _MAX_LINEAGE_CANDIDATES
                    ):
                        lineage_root_fqns_added.add(root)
                        _try_add_lineage_candidate(root, fqn)

        entry = {
            "table_fqn": fqn,
            "object_type": object_type,
            "business_name": (ctx.get("dictionary") or {}).get("business_name"),
            "description": (ctx.get("dictionary") or {}).get("description"),
            "columns": [
                {
                    "column_name": c["column_name"],
                    "data_type": (c.get("schema") or {}).get("data_type"),
                    "is_primary_key": bool((c.get("schema") or {}).get("is_primary_key")),
                    "is_metric": _is_metric_column(c),
                    "is_dimension": _is_dimension_column(c),
                    "is_date": bool((c.get("dictionary") or {}).get("is_date")),
                }
                for c in columns[:_MAX_EVIDENCE_COLUMNS]
            ],
            "primary_key": _select_entity_key(ctx),
            "approx_row_count": approx_row_count,
            "max_column_uniqueness": max(
                (((c.get("profiling") or {}).get("uniqueness_score") or 0) for c in columns), default=0,
            ),
            "is_root_table": bool((ctx.get("profiling") or {}).get("is_root_table")),
            "is_leaf_table": bool((ctx.get("profiling") or {}).get("is_leaf_table")),
            "relationships": [
                {"table_fqn": r["table_fqn"], "relationship_types": r.get("relationship_types"),
                 "confidence": r.get("confidence")}
                for r in (related.get("related_tables") or [])[:_MAX_EVIDENCE_RELATIONSHIPS]
            ],
            "domain": (ctx.get("domain") or {}).get("domain"),
            "entity": (ctx.get("entity") or {}).get("entity"),
            "status_evidence": status_evidence,
            "derivative_flag": penalty_reason,
            "name_score": cand.get("name_score"),
            "score": cand.get("score"),
            "deterministic_rank": deterministic_rank_by_fqn.get(fqn),
            "candidate_business_role": business_role,
            "human_readable_descriptor_columns": descriptor_columns,
            "person_reference_columns": person_ref_columns,
            "authority_reasons": (cand.get("ranking_reasons") or [])[:_MAX_EVIDENCE_COLUMNS],
            "restricted_view_evidence": restricted_view_evidence,
            "lineage_source_view": lineage_source_view,
            "lineage_root_fqn": lineage_root_fqn,
            # Business Learning Candidate Discovery milestone, Part 5 — see
            # preliminary_provenance_by_fqn's own comment above. None for
            # every candidate found by any validated evidence source
            # (the overwhelming majority) — only set when THIS call's own
            # retrieval found this specific table SOLELY via the
            # unvalidated preliminary business map.
            "candidate_provenance": preliminary_provenance_by_fqn.get(fqn),
        }
        # View Semantic Discovery milestone — only added for a VIEW
        # candidate that actually has resolved dependencies, never as a
        # blanket null field on every candidate: keeps every non-VIEW
        # candidate's evidence shape byte-identical to before this
        # milestone, since even an inert extra key changes the exact JSON
        # sent to the AI adjudicator's prompt.
        if referenced_tables_evidence:
            entry["referenced_tables"] = referenced_tables_evidence
        evidence.append(entry)
        return entry

    def _try_add_lineage_candidate(root_fqn: str, source_view_fqn: str) -> None:
        # A syntactic guess from _extract_lineage_root_table — re-verified
        # against this source's own real catalog exactly like every other
        # candidate here (get_table_business_context returns None for
        # anything that isn't a real, owned table), never trusted blindly.
        root_ctx = get_table_business_contexts_batch(source_id, user_id, [root_fqn], session=session).get(root_fqn)
        if not root_ctx:
            lineage_root_fqns_added.discard(root_fqn)
            return
        # score/name_score must stay real floats, never None — several
        # downstream consumers of candidates_by_fqn (e.g. _tied_top_
        # candidates) sort by -score unconditionally. 0.0 is a safe,
        # honest default: this candidate's authority comes from lineage
        # evidence, not from _resolve_concept's own lexical/vocabulary
        # scoring, so it claims no lexical score of its own.
        synthetic_cand = {
            "table_fqn": root_fqn, "score": 0.0, "name_score": 0.0, "ranking_reasons": [
                f"Structural lineage: {source_view_fqn}'s own FROM clause is built on this table.",
            ],
            "lineage_source_view": source_view_fqn,
        }
        _build_evidence_entry(root_fqn, synthetic_cand, root_ctx, lineage_source_view=source_view_fqn)

    # PERFORMANCE PHASE C — bounded, read-only prefetch of the two
    # restricted-view live probes (row_count/view_definition) for every
    # VIEW-typed candidate in to_inspect, run CONCURRENTLY instead of one
    # at a time. This is purely a cache-warming optimization: it mutates
    # NOTHING except the SAME session-scoped probe cache Phase A+B already
    # built (_probe_view_row_count/_probe_view_definition, unchanged) --
    # never evidence/candidates_by_fqn/lineage_root_fqns_added, and never
    # decides anything about restriction/lineage/disqualification itself.
    # The serial loop directly below is completely unmodified: when it
    # reaches a prefetched candidate, its own _probe_view_row_count/
    # _probe_view_definition calls simply become cache hits instead of
    # fresh live round trips -- same order, same recursion, same mutation,
    # same exception handling as before this milestone. Only genuinely
    # distinct VIEW candidates are ever submitted concurrently here (a
    # candidate reachable from TWO different table_fqns would still be two
    # separate, independent probes -- there are no duplicates to begin with
    # within one to_inspect batch, by construction of inspected_fqns above).
    if session is not None:
        _prefetch_tasks: list[tuple[str, bool]] = []
        for _cand in to_inspect:
            _fqn = _cand["table_fqn"]
            _ctx = table_contexts.get(_fqn)
            if _ctx is None:
                continue
            if (_ctx.get("table") or {}).get("table_type") != "VIEW":
                continue
            _approx_row_count = (
                (_ctx.get("profiling") or {}).get("exact_row_count")
                or (_ctx.get("profiling") or {}).get("estimated_row_count")
            )
            _prefetch_tasks.append((_fqn, _approx_row_count is None))

        if _prefetch_tasks:
            def _warm_one_view_probe(fqn: str, needs_row_count: bool) -> None:
                # inspect_targeted_values (via _probe_view_row_count/
                # _probe_view_definition) never raises -- it fails closed to
                # InvestigationResult(valid=False) on any timeout/permission/
                # connection/malformed-result condition, exactly like a
                # synchronous call in the serial loop would. This try/except
                # is only a backstop for something outside that contract
                # (e.g. the worker thread itself being killed) -- a prefetch
                # failure here just means that ONE candidate isn't pre-
                # warmed; the serial loop below still probes it fresh,
                # identically to running without this prefetch step at all.
                try:
                    if needs_row_count:
                        _probe_view_row_count(
                            source_id, user_id, fqn,
                            reason=f"Semantic grounding: profiling row count for VIEW candidate {fqn}.",
                            session=session,
                        )
                    _probe_view_definition(
                        source_id, user_id, fqn,
                        reason=f"Semantic grounding: checking {fqn} for restriction/lineage evidence.",
                        session=session,
                    )
                except Exception:  # noqa: BLE001 -- a prefetch optimization must never break discovery
                    logger.debug(
                        "semantic_contract_service: VIEW probe prefetch failed for %s", fqn, exc_info=True,
                    )

            from concurrent.futures import ThreadPoolExecutor

            _worker_count = min(_MAX_VIEW_PROBE_WORKERS, len(_prefetch_tasks))
            _batch_t0 = _time.monotonic()
            with ThreadPoolExecutor(max_workers=_worker_count) as _pool:
                _futures = [_pool.submit(_warm_one_view_probe, fqn, needs_rc) for fqn, needs_rc in _prefetch_tasks]
                for _f in _futures:
                    _f.result()  # block here -- no worker may outlive this batch or this request
            stage_timer.record("view_probe_parallel_batch", (_time.monotonic() - _batch_t0) * 1000)

    for cand in to_inspect:
        fqn = cand["table_fqn"]
        ctx = table_contexts.get(fqn)
        if ctx is None:
            continue
        _build_evidence_entry(fqn, cand, ctx)

    stage_timer.record("gather_candidate_evidence_inspect_loop", (_time.monotonic() - _inspect_loop_t0) * 1000)
    stage_timer.record("gather_candidate_evidence", (_time.monotonic() - _gce_t0) * 1000)
    return {
        "entity_name": entity_name, "synonyms": list(synonyms),
        "pool_table_fqns": pool_table_fqns, "evidence": evidence, "candidates_by_fqn": candidates_by_fqn,
    }


# ---------------------------------------------------------------------------
# Automatic Multi-Entity Semantic Grounding, Part 3/5 — Joint Entity
# Resolution / Automatic Relationship Use. A dynamic entity candidate's own
# deterministic discovery can land on a genuine, close-margin ambiguity
# (e.g. "instructor" scoring 0.62 for dbo.EDU_Instructor vs 0.53 for
# dbo.InstructorCommissions — both plausible lexical matches, neither a
# hopeless mismatch) purely from name-based scoring, which has no way to see
# that only ONE of the two actually has a trusted join path to an entity
# this SAME question already grounded. That connectivity is exactly the
# corroborating evidence Part 3 describes: "a weak standalone lexical match
# must not beat a strongly-connected business entity pair."
# ---------------------------------------------------------------------------

def _tied_top_candidates(source_id: int, user_id: str, entity_name: str, *, session=None) -> list[dict] | None:
    """The near-tied top candidates from gather_candidate_evidence's own
    bounded pool (the SAME evidence the AI-adjudication fallback already
    uses — no new discovery algorithm), or None when this isn't the
    close-margin-ambiguity shape _relationship_disambiguate/_joint_
    disambiguate_pair exist for (a hopeless case below the confidence
    floor, or a single clear winner with no real tie). Never raises."""
    from data.query_planning_service import _AUTO_SELECT_MIN_CONFIDENCE, _AMBIGUITY_MARGIN

    try:
        evidence = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    except Exception:  # noqa: BLE001
        logger.debug(
            "_tied_top_candidates: evidence gathering failed for entity_name=%s", entity_name, exc_info=True,
        )
        return None
    candidates_by_fqn = evidence.get("candidates_by_fqn") or {}
    if not candidates_by_fqn:
        return None
    ranked = sorted((c["candidate"] for c in candidates_by_fqn.values()), key=lambda c: -c["score"])
    if not ranked or ranked[0]["score"] < _AUTO_SELECT_MIN_CONFIDENCE:
        return None
    top_score = ranked[0]["score"]
    tied = [c for c in ranked if (top_score - c["score"]) <= _AMBIGUITY_MARGIN]
    if len(tied) < 2:
        return None
    return tied


# A dynamic entity's own corroborating relationship must be a SHORT,
# direct-ish path (at most one intermediate table) — live-confirmed
# against a real production database that a longer chain (e.g. dbo.EDU_Instructor ->
# dnnuser.Users -> dbo.RetoolEmailEngagement -> dbo.Emails, 3 hops) is
# reachable but not a genuine business relationship, and would otherwise
# let a plain attribute word ("emails") get promoted to a whole second
# relationship ENTITY instead of being resolved as an attribute of the
# entity already grounded (a distinct, already-tracked gap — D3 — that
# this tie-break must never worsen). recommend_best_join_path itself is
# NOT restricted by this — _plan_joins and every other caller keep using
# its full, unmodified result; this bound applies only to the NEW
# corroboration signal this milestone adds.
_MAX_TIEBREAK_HOPS = 2


def _trusted_connection_path(source_id: int, user_id: str, table_a: str, table_b: str) -> list[str] | None:
    """The best trusted (AUTO/APPROVED-only), SHORT (<= _MAX_TIEBREAK_HOPS
    hops) join path's table sequence (table_a, ...intermediates..., table_b)
    between the two tables, or None when no such path exists. Same
    trusted-path primitive _trusted_connection/_plan_joins already use
    (recommend_best_join_path) — this variant additionally exposes which
    tables the path actually passes through, so a caller can tell a direct/
    independent connection apart from one that merely routes through
    another table it already cares about. Never raises."""
    from data.semantic_layer_service import recommend_best_join_path

    try:
        path = recommend_best_join_path(source_id, user_id, table_a, table_b)
    except Exception:  # noqa: BLE001
        return None
    best = path.get("best_join_path") if path else None
    if not best:
        return None
    hops = best.get("hops")
    if hops is None or hops > _MAX_TIEBREAK_HOPS:
        return None
    # Falls back to a synthetic direct (no-intermediate) edge when the
    # caller's own best_join_path result doesn't carry path detail (e.g. a
    # test double built before this field was consumed here) — treated as
    # "connected, no intermediate to check" rather than "not connected".
    return best.get("path") or [table_a, table_b]


def _trusted_connection(source_id: int, user_id: str, table_a: str, table_b: str) -> bool:
    """True iff a trusted (AUTO/APPROVED-only — recommend_best_join_path's
    own, unmodified trust filter), SHORT (<= _MAX_TIEBREAK_HOPS hops) join
    path exists between the two tables. Never raises."""
    return _trusted_connection_path(source_id, user_id, table_a, table_b) is not None


def _build_tiebreak_contract(
    source_id: int, entity_name: str, table_fqn: str, score: float, note: str, *, session=None,
) -> dict:
    """Contract dict in the SAME shape get_or_build_entity_contract
    returns, for a relationship-corroborated tie-break result. Deliberately
    never persisted via persist_entity_contract — see _relationship_
    disambiguate's own docstring for why."""
    metadata_revision = current_metadata_revision(source_id, session=session)
    doc = _empty_contract_document(entity_name)
    doc["evidence"] = [note]
    return {
        "source_id": source_id, "entity_name": entity_name, "contract_version": None,
        "canonical_table_fqn": table_fqn, "preferred_view_fqn": None,
        "resolution_status": "PARTIAL", "confidence": score,
        "contract": doc, "metadata_revision": metadata_revision,
        "generation_method": "relationship_tiebreak",
        "created_at": None, "updated_at": None,
    }


def _relationship_disambiguate(
    source_id: int, user_id: str, entity_name: str, other_table_fqns: set[str], *, session=None,
) -> dict | None:
    """Bounded, per-question tie-break for a dynamic entity candidate whose
    deterministic discovery found several near-tied top candidates
    (_tied_top_candidates). Promotes the tie only when EXACTLY ONE of the
    tied candidates has a TRUSTED join path (_trusted_connection — the same
    trusted-path finder _plan_joins already uses, never a new one) to any
    table in `other_table_fqns`; two-or-more equally-connected candidates,
    or zero, still safely refuse by returning None — never a guess.

    Returns a contract dict in the SAME shape get_or_build_entity_contract
    returns (so callers use it identically), or None. Deliberately never
    persisted via persist_entity_contract: this judgment depends on which
    OTHER entities happen to co-occur in THIS question, not a source-wide
    fact about entity_name — a future question naming entity_name alone,
    or alongside different entities, must not be pinned to today's
    tie-break. Never raises; any failure returns None (falls through to the
    ordinary, unchanged legacy per-term resolution path for this term).
    """
    if not other_table_fqns:
        return None
    tied = _tied_top_candidates(source_id, user_id, entity_name, session=session)
    if tied is None:
        return None

    # A tied candidate that is literally THE SAME table as one of the
    # anchors (e.g. dbo.EDU_ClassSignups tied for "Class" while also being
    # the already-grounded anchor table for "Enrollment") produces a
    # trivial same-table path (recommend_best_join_path's own hops=0
    # identity case) — that is not real relationship evidence about
    # entity_name's own identity, just the anchor already being counted for
    # a different entity, and must never itself count as a competing
    # connection.
    connected = [
        (cand, path) for cand in tied
        for path in [_trusted_connection_path(source_id, user_id, cand["table_fqn"], other_fqn)
                     for other_fqn in other_table_fqns if cand["table_fqn"] != other_fqn]
        if path is not None
    ]
    # tied[0] is the tied set's own top LEXICAL scorer (_tied_top_candidates
    # returns it score-sorted). Live-confirmed against a real production database why
    # connectivity may only CONFIRM that top scorer, never PROMOTE a lower
    # one over it: dbo.EDU_Instructor is a weakly-connected "hub" reachable
    # from dozens of tables via a coincidental shared dbo.EDU_Cancel lookup
    # column, so connectivity alone can make a worse lexical match (0.73)
    # look "connected" while a clearly better one (0.87) merely falls
    # outside the search — without this guard that would silently override
    # a near-conclusive lexical match with a worse one.
    if not any(cand is tied[0] for cand, _path in connected):
        return None

    # Consistent Automatic Business-Term Resolution — a second connected
    # candidate does not always mean genuine competing evidence. Live-
    # confirmed against a real production database: dbo.EDU_ClassSignups "connects" to
    # dbo.EDU_Track only by routing straight THROUGH the incumbent
    # dbo.EDU_Class (ClassSignups -> Class -> Path) — that is not
    # independent corroboration for a different table, just an echo of the
    # SAME evidence that already connects the incumbent, and previously
    # blocked confirming the incumbent outright. A connection is still
    # treated as genuinely independent competing evidence — and still
    # safely refuses, exactly as before — whenever its own path does NOT
    # pass through the incumbent's own table (e.g. two separately-reachable
    # candidates, or a candidate reachable via an unrelated route).
    incumbent_fqn = tied[0]["table_fqn"]
    independent_others = [
        cand for cand, path in connected
        if cand is not tied[0] and incumbent_fqn not in path[1:-1]
    ]
    if independent_others:
        return None
    winner = tied[0]
    note = (
        f"{len(tied)} lexically near-tied candidates (within the ordinary auto-select margin); "
        f"resolved via a trusted relationship path from '{winner['table_fqn']}' to an entity already "
        f"grounded in this question (Automatic Multi-Entity Semantic Grounding, Part 3)."
    )
    return _build_tiebreak_contract(source_id, entity_name, winner["table_fqn"], winner["score"], note, session=session)


def _joint_disambiguate_pair(
    source_id: int, user_id: str,
    entity_a: str, tied_a: list[dict], entity_b: str, tied_b: list[dict],
    *, session=None,
) -> tuple[dict, dict] | None:
    """Part 3 — mutual Joint Entity Resolution. When TWO dynamic candidates
    are BOTH independently ambiguous (neither has any other already-
    grounded entity to corroborate against — e.g. "Show classes and
    instructors" with no fixed-taxonomy entity in the question at all),
    they may still corroborate EACH OTHER: every combination of their own
    tied top candidates is checked for a trusted join path; promotes BOTH
    only when EXACTLY ONE combination connects AND that combination pairs
    each entity's OWN top LEXICAL scorer (tied_a[0]/tied_b[0] — same
    "connectivity may only confirm the top scorer, never promote a lower
    one over it" guard _relationship_disambiguate applies, and for the
    identical live-confirmed reason: a weakly-connected hub table can make
    a worse lexical match look connected while a clearly better one falls
    just outside the search). Two-or-more equally-connected combinations,
    zero, or a connection that isn't both sides' top scorer, all still
    safely refuse. Same trusted-path primitive as _relationship_
    disambiguate, applied pairwise instead of against a fixed anchor set.
    Never raises."""
    connections = [
        (ca, cb) for ca in tied_a for cb in tied_b
        if _trusted_connection(source_id, user_id, ca["table_fqn"], cb["table_fqn"])
    ]
    if len(connections) != 1:
        return None
    ca, cb = connections[0]
    if ca is not tied_a[0] or cb is not tied_b[0]:
        return None
    note = (
        f"Mutually ambiguous lexical candidates for '{entity_a}' and '{entity_b}' — neither had another "
        f"already-grounded entity to corroborate against — resolved via a trusted relationship path "
        f"between their own top candidates (Automatic Multi-Entity Semantic Grounding, Part 3)."
    )
    return (
        _build_tiebreak_contract(source_id, entity_a, ca["table_fqn"], ca["score"], note, session=session),
        _build_tiebreak_contract(source_id, entity_b, cb["table_fqn"], cb["score"], note, session=session),
    )



# Deliberately a separate, slightly deeper bound than _MAX_TIEBREAK_HOPS
# (2): that constant guards a WEAKER decision (picking among candidates
# that already independently cleared the lexical auto-select floor, where
# raw reachability through a generic hub table alone was shown live to be
# misleading — see _MAX_TIEBREAK_HOPS's own docstring). This rescue's bar is
# strictly stronger before hop distance is even consulted — it requires
# real HIGH/MEDIUM vocabulary evidence AND exactly one such table reachable
# at all — so a hub-table false positive would need to coincidentally be
# BOTH the sole vocabulary-evidenced table for this term AND reachable,
# which a generic fan-out hub is structurally unlikely to be. Calibrated to
# real schema depth: an association/bridge-table join chain (entity ->
# enrollment/signup -> class -> program, a completely ordinary hub-and-
# spoke shape) is 3 hops, not 2 — confirmed live against a real production database
# (dbo.EDU_Student -> dbo.EDU_ClassSignups -> dbo.EDU_Class -> dbo.EDU_Track).
_RESCUE_MAX_HOPS = 3


def _relationship_rescued_candidate(
    source_id: int, user_id: str, entity_name: str, other_table_fqns: set[str], *, session=None,
) -> dict | None:
    """
    Enterprise Phase 4 — Automatic Business Vocabulary. Last-resort rescue
    for a dynamic entity term whose deterministic discovery found NO
    candidate at all (gather_candidate_evidence's own pool was empty, or
    everything in it scored below the auto-select floor) — distinct from
    _relationship_disambiguate above, which only ever picks AMONG
    candidates lexical/vocabulary search already retrieved.

    A business word sometimes attaches to the RIGHT table only through a
    single column's name (e.g. a "ProgramType" column on a table, dbo.
    EDU_Track, whose own name shares no words with "program" at all) —
    data.vocabulary_bootstrap_service records this as real evidence, but
    deliberately weak, column-scoped evidence alone never promotes a table
    into the ordinary candidate pool (see
    semantic_retrieval_service._is_column_name_only_evidence's own
    docstring for why: a coincidental column-name match on an unrelated
    table must not inject it). When that same table ALSO sits on a short,
    trusted (AUTO/APPROVED) join path from an entity this question has
    ALREADY grounded, the two independently-weak signals corroborate each
    other — exactly the same "a weak standalone match must not beat a
    strongly-connected business entity pair" principle
    _relationship_disambiguate's own module docstring already establishes,
    applied one step earlier: before any lexical candidate existed to tie.

    Promotes only when EXACTLY ONE table has real (HIGH/MEDIUM tier)
    generated-vocabulary evidence for entity_name's synonyms AND sits
    within _RESCUE_MAX_HOPS of `other_table_fqns` — two-or-more equally-
    evidenced-and-connected tables, or zero, still safely return None,
    never a guess. Never raises.

    Unlike _relationship_disambiguate/_joint_disambiguate_pair (deliberately
    never persisted — their judgment depends on which OTHER candidates
    happened to be lexically tied in THIS question), a rescue here is
    persisted via persist_entity_contract when it succeeds: it does not
    depend on what else was tied, only on whether a business term has real,
    source-wide vocabulary evidence pointing at a table this source's own
    relationship graph confirms is on a trusted path from a grounded
    business entity — a fact that holds regardless of which question first
    surfaced it, and is automatically invalidated/recomputed the moment
    metadata_revision changes (schema/relationship/vocabulary refresh), the
    same as every other contract here. This is what lets a later, STANDALONE
    question naming the term alone (no other entity to jointly resolve
    against, e.g. "Show programs") benefit from a resolution first
    discovered by an earlier, richer question (e.g. "How many students are
    in each program?") — automatic learning, no human approval, source-
    scoped, confidence and provenance recorded in contract_json['evidence'].
    """
    if not other_table_fqns:
        return None
    try:
        from data.vocabulary_bootstrap_service import get_generated_vocabulary
        from data.vocabulary_service import normalize_term
        from data.semantic_retrieval_service import get_relationship_distance_map
        from data.business_knowledge_service import get_table_business_context

        vocabulary = get_generated_vocabulary(source_id, session=session)
        if not vocabulary:
            return None

        evidenced_tables: set[str] = set()
        for synonym in entity_synonyms(entity_name):
            key = normalize_term(synonym)
            if not key:
                continue
            for row in vocabulary.get(key) or []:
                if row.get("confidence_tier") in ("HIGH", "MEDIUM") and row.get("table_fqn"):
                    evidenced_tables.add(row["table_fqn"])
        if not evidenced_tables:
            return None

        distances = get_relationship_distance_map(
            source_id, other_table_fqns, max_hops=_RESCUE_MAX_HOPS, session=session,
        )
        connected = sorted(
            (t for t in evidenced_tables if distances.get(t, 0) > 0),
            key=lambda t: distances[t],
        )
        if len(connected) != 1:
            return None

        table_fqn = connected[0]
        ctx = get_table_business_context(source_id, user_id, table_fqn, session=session)
        if not ctx:
            return None  # never invent a table outside the real, hydrated metadata

        note = (
            f"No standalone lexical/vocabulary candidate cleared the ordinary auto-select bar for "
            f"'{entity_name}'; resolved via generated-vocabulary evidence on '{table_fqn}' "
            f"(hop distance {distances[table_fqn]}) corroborated by a trusted relationship path to "
            f"an entity already grounded in this question (Automatic Business Vocabulary, joint "
            f"relationship-aware resolution)."
        )
        contract = _build_tiebreak_contract(source_id, entity_name, table_fqn, 0.75, note, session=session)

        # Persist (unlike the tie-break functions above) so a future
        # question naming this term alone gets a normal cache hit instead
        # of needing another grounded entity to rescue against every time.
        try:
            persisted = persist_entity_contract(
                source_id, entity_name,
                canonical_table_fqn=contract["canonical_table_fqn"],
                preferred_view_fqn=contract["preferred_view_fqn"],
                resolution_status=contract["resolution_status"], confidence=contract["confidence"],
                contract_json=contract["contract"], metadata_revision=contract["metadata_revision"],
                generation_method="relationship_rescue", session=session,
            )
            return persisted
        except Exception:  # noqa: BLE001 - the ephemeral contract is still usable this request
            logger.warning(
                "_relationship_rescued_candidate: persistence failed for source_id=%s entity_name=%s",
                source_id, entity_name, exc_info=True,
            )
            return contract
    except Exception:  # noqa: BLE001
        logger.debug(
            "_relationship_rescued_candidate: rescue failed for source_id=%s entity_name=%s",
            source_id, entity_name, exc_info=True,
        )
        return None


def _discover_entity_contract_via_adjudication(
    source_id: int, user_id: str, entity_name: str, *, session=None,
) -> dict | None:
    """One bounded AI-assisted adjudication pass. Returns None (never a
    partial/unsafe result) whenever the AI call fails/is disabled, returns
    NO_SAFE_SELECTION, selects something outside the evidence it was given,
    or fails Task 1's deterministic validation — callers must treat None as
    "keep the deterministic NO_CANDIDATE result"."""
    from core.semantic.candidate_adjudicator import adjudicate_candidates

    metadata_revision = current_metadata_revision(source_id, session=session)
    gathered = gather_candidate_evidence(source_id, user_id, entity_name, session=session)
    if not gathered["evidence"]:
        return None

    result = adjudicate_candidates(entity_name, gathered["evidence"])
    if result is None:
        return None

    if result.no_safe_selection or not result.selected_table_fqn:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append(
            "AI candidate adjudication found no safe selection among "
            f"{len(gathered['evidence'])} inspected candidate(s).",
        )
        doc["excluded_objects"] = [
            {"table_fqn": ec.get("table_fqn"), "reason": ec.get("reason")}
            for ec in (result.excluded_candidates or ())
        ]
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_SAFE_SELECTION", "confidence": result.confidence,
            "contract_json": doc, "metadata_revision": metadata_revision,
            "generation_method": "ai_adjudicated",
        }

    picked = gathered["candidates_by_fqn"].get(result.selected_table_fqn)
    if picked is None:
        # The adjudicator's own hard identifier check makes this
        # unreachable in practice — fail closed rather than trust an
        # out-of-pool selection either way.
        return None

    # A preferred analytical view (e.g. a separate current-status view over
    # a base membership table — the real shape found revalidating Launch
    # Participant) is a second trusted evidence object: status/date
    # semantics may legitimately live there instead of on the canonical
    # table, but grain/identity/relationships/derivative checks stay scoped
    # to the canonical object alone.
    view_picked = (
        gathered["candidates_by_fqn"].get(result.preferred_analytical_view_fqn)
        if result.preferred_analytical_view_fqn else None
    )
    view_columns = (view_picked["ctx"].get("columns") or []) if view_picked else []
    view_column_names = {c["column_name"] for c in view_columns}

    claimed_date_columns = [c for c in (result.date_semantics or {}).values() if c]
    validation = validate_candidate_contract(
        entity_name, picked["candidate"], picked["ctx"],
        requested_attributes=list(result.key_attributes or ()),
        claimed_status_column=(result.status_semantics or {}).get("column_name"),
        claimed_date_columns=claimed_date_columns,
        value_samples=(result.status_semantics or {}).get("verified_values"),
        intentional_analytical=bool(
            result.preferred_analytical_view_fqn
            and result.preferred_analytical_view_fqn == result.selected_table_fqn
        ),
        additional_valid_columns=view_column_names,
    )
    if not validation.accepted:
        doc = _empty_contract_document(entity_name)
        doc["evidence"].append(
            f"AI-selected candidate '{result.selected_table_fqn}' failed deterministic "
            f"validation: {'; '.join(validation.reasons)}",
        )
        return {
            "canonical_table_fqn": None, "preferred_view_fqn": None,
            "resolution_status": "NO_SAFE_SELECTION", "confidence": result.confidence,
            "contract_json": doc, "metadata_revision": metadata_revision,
            "generation_method": "ai_adjudicated",
        }

    ctx = picked["ctx"]
    columns = ctx.get("columns") or []
    column_names = {c["column_name"] for c in columns}

    def _owning_table_and_column_ctx(col_name: str) -> tuple[str, dict | None]:
        """Which of the canonical table or the preferred view actually
        carries this column — status/date semantics record the real owner
        rather than always assuming the canonical table."""
        if col_name in column_names:
            return result.selected_table_fqn, next((c for c in columns if c["column_name"] == col_name), None)
        return result.preferred_analytical_view_fqn, next(
            (c for c in view_columns if c["column_name"] == col_name), None,
        )

    status_semantics = None
    status_col_name = (result.status_semantics or {}).get("column_name")
    if status_col_name and (status_col_name in column_names or status_col_name in view_column_names):
        owner_fqn, col_ctx = _owning_table_and_column_ctx(status_col_name)
        status_semantics = {
            "table_fqn": owner_fqn,
            "column_name": status_col_name,
            "data_type": ((col_ctx or {}).get("schema") or {}).get("data_type"),
            "verified_values": list(result.status_semantics.get("verified_values") or ()),
            "value_source": "ai_adjudication_evidence" if result.status_semantics.get("verified_values") else None,
        }
    date_semantics = {
        role: {"table_fqn": _owning_table_and_column_ctx(col)[0], "column_name": col}
        for role, col in (result.date_semantics or {}).items()
        if col in column_names or col in view_column_names
    }

    resolution_status = "PARTIAL" if status_semantics and not status_semantics["verified_values"] else "RESOLVED"
    contract_json = {
        "entity_name": entity_name,
        "synonyms": list(gathered["synonyms"]),
        "source_system_label": (ctx.get("source") or {}).get("display_name") or (ctx.get("source") or {}).get("source_type"),
        "grain": result.grain or (ctx.get("dictionary") or {}).get("grain") or f"One row per {entity_name}.",
        "primary_key": (
            {"table_fqn": result.selected_table_fqn, "column_name": result.identity_key}
            if result.identity_key and result.identity_key in column_names else None
        ),
        "attributes": [
            {"business_name": (c.get("dictionary") or {}).get("business_label") or c["column_name"],
             "column_name": c["column_name"]}
            for c in columns if c["column_name"] in (result.key_attributes or ())
        ],
        "measures": [],
        "status_semantics": status_semantics,
        "date_semantics": date_semantics,
        "relationships": [{"table_fqn": r} for r in (result.relationships or ()) if r != result.selected_table_fqn],
        "excluded_objects": [
            {"table_fqn": ec.get("table_fqn"), "reason": ec.get("reason")}
            for ec in (result.excluded_candidates or ())
        ],
        "alternate_candidates": [
            fqn for fqn in gathered["pool_table_fqns"]
            if fqn not in (result.selected_table_fqn, result.preferred_analytical_view_fqn)
        ][:_MAX_ALTERNATE_CANDIDATES],
        "evidence": [
            f"AI-adjudicated from {len(gathered['evidence'])} inspected candidate(s) "
            f"(pool of {len(gathered['pool_table_fqns'])}).",
            *(f"Deterministic validation warning: {w}" for w in validation.warnings),
            *list(result.evidence_references or ()),
        ],
    }

    return {
        "canonical_table_fqn": result.selected_table_fqn,
        "preferred_view_fqn": result.preferred_analytical_view_fqn,
        "resolution_status": resolution_status, "confidence": result.confidence,
        "contract_json": contract_json, "metadata_revision": metadata_revision,
        "generation_method": "ai_adjudicated",
    }


def discover_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Day 2C discovery entry point. Runs the deterministic pass first
    (_discover_entity_contract_deterministic, unchanged Day 2B logic); when
    that lands on NO_CANDIDATE and ENABLE_AI_CANDIDATE_ADJUDICATION is on,
    falls back to one bounded AI-assisted adjudication pass (Task 2),
    deterministically re-validated (Task 1) before ever being treated as a
    real result. AI failure/unavailability/schema violation/failed
    validation all fail closed to the original deterministic NO_CANDIDATE
    result — this function never behaves worse than Day 2B did.
    """
    result = _discover_entity_contract_deterministic(source_id, user_id, entity_name, session=session)
    result.setdefault("generation_method", "auto_grounding")
    if result["resolution_status"] != "NO_CANDIDATE":
        return result

    from core.config import ENABLE_AI_CANDIDATE_ADJUDICATION
    if not ENABLE_AI_CANDIDATE_ADJUDICATION:
        return result

    adjudicated = _discover_entity_contract_via_adjudication(source_id, user_id, entity_name, session=session)
    return adjudicated if adjudicated is not None else result


# ---------------------------------------------------------------------------
# Task 6 — cache by (source_id, entity_name, metadata_revision). This is
# the ONLY write path at query time: fully automatic, no approval step.
# ---------------------------------------------------------------------------

def get_or_build_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict:
    """Latest contract, rebuilt (and persisted as a new version) only when
    stale or absent. A fresh cache hit does no discovery work at all — no
    candidate search, no context hydration, no AI call."""
    revision = current_metadata_revision(source_id, session=session)
    latest = get_latest_entity_contract(source_id, entity_name, session=session)
    if latest is not None and latest["metadata_revision"] == revision:
        return latest

    built = discover_entity_contract(source_id, user_id, entity_name, session=session)
    persist_entity_contract(
        source_id, entity_name,
        canonical_table_fqn=built["canonical_table_fqn"], preferred_view_fqn=built["preferred_view_fqn"],
        resolution_status=built["resolution_status"], confidence=built["confidence"],
        contract_json=built["contract_json"], metadata_revision=built["metadata_revision"],
        generation_method=built.get("generation_method", "auto_grounding"),
        session=session,
    )
    return get_latest_entity_contract(source_id, entity_name, session=session)


# ---------------------------------------------------------------------------
# Day 2C, Task 3 — one-off forced revalidation.
# ---------------------------------------------------------------------------

def revalidate_entity_contract(source_id: int, user_id: str, entity_name: str, *, session=None) -> dict | None:
    """Forces one bounded AI-assisted adjudication pass for entity_name
    regardless of the deterministic pass's own resolution_status or the
    contract cache's freshness.

    Unlike discover_entity_contract's own AI fallback (which only runs when
    the deterministic pass lands on NO_CANDIDATE), this ALWAYS runs
    adjudication — for one-off revalidation of a contract suspected wrong
    even though it is currently RESOLVED (Day 2C Task 3: Pilot Participant
    resolved to the exact same table and confidence score as Student —
    strong evidence of a degenerate match, not a genuine answer). The
    deterministic top candidate is not treated specially here: it is just
    one more entry in gather_candidate_evidence's own evidence pool, and
    Task 1's validate_candidate_contract is still the only thing that can
    accept a new canonical table.

    Always persists a new contract_version (never overwrites — same
    versioning idiom as every other write here) when adjudication produces
    a result, including an explicit NO_SAFE_SELECTION downgrade of a
    previously-RESOLVED contract. Returns the current latest contract
    unchanged (no new version written) when ENABLE_AI_CANDIDATE_ADJUDICATION
    is off or the AI call itself fails/is unavailable — there is then
    nothing new to persist.
    """
    from core.config import ENABLE_AI_CANDIDATE_ADJUDICATION
    if not ENABLE_AI_CANDIDATE_ADJUDICATION:
        return get_latest_entity_contract(source_id, entity_name, session=session)

    adjudicated = _discover_entity_contract_via_adjudication(source_id, user_id, entity_name, session=session)
    if adjudicated is None:
        return get_latest_entity_contract(source_id, entity_name, session=session)

    persist_entity_contract(
        source_id, entity_name,
        canonical_table_fqn=adjudicated["canonical_table_fqn"],
        preferred_view_fqn=adjudicated["preferred_view_fqn"],
        resolution_status=adjudicated["resolution_status"], confidence=adjudicated["confidence"],
        contract_json=adjudicated["contract_json"], metadata_revision=adjudicated["metadata_revision"],
        generation_method=adjudicated.get("generation_method", "ai_adjudicated"),
        session=session,
    )
    return get_latest_entity_contract(source_id, entity_name, session=session)


# ---------------------------------------------------------------------------
# General Attribute Resolution — a dynamic entity candidate must not be
# promoted over real evidence it instead names an ATTRIBUTE of an entity
# this question already grounded. Real-the reference database regression: "Show student
# emails" grounds "Student" (fixed taxonomy) fine, but "Email" ALSO clears
# match_dynamic_entity_terms's table-level bar (dbo.Emails, an unrelated
# inbox/message-log table whose own NAME happens to match) and gets
# promoted to a second grounded entity, hijacking _try_build_relationship_
# plan into treating "Student"/"Email" as two entities to relate — instead
# of ever reaching the (already-general, non-hardcoded) related-attribute
# fallback that would have found the REAL match, dnnuser.Users.Email, one
# trusted hop from dbo.EDU_Student. Same pattern for "instructor city"
# (dbo.EDU_City, an unrelated lookup table, vs. the real
# dbo.EDU_Instructor.InstructorCity column that already lives directly on
# the grounded table).
# ---------------------------------------------------------------------------

def _term_matches_grounded_table_column(
    source_id: int, user_id: str, term: str, table_fqns: "set[str]", *, session=None,
) -> bool:
    """True when `term` scores as a real COLUMN — the same _score_term_match
    / _AUTO_SELECT_MIN_CONFIDENCE bar every other column resolution in this
    codebase already uses — on any of `table_fqns` (tables already grounded
    earlier in THIS question) or one of their own trusted (AUTO/APPROVED)
    1-hop related tables (get_table_business_context's own relationships
    field, already trust-filtered — no new relationship discovery).

    Consulted as a veto before promoting a dynamic entity candidate whose
    only evidence for `term` is a coincidental TABLE-name match
    (match_dynamic_entity_terms's own table-level bar) — real column-level
    evidence on an already-grounded entity is stronger, more specific
    proof that `term` names an ATTRIBUTE of that entity, not a genuine
    second one. Bounded to already-grounded tables plus their own
    already-loaded 1-hop relationships; no broad search, no new evidence
    source. Never raises.

    Persistence Consistency milestone, Part 1/2 fix — EXCLUDES a candidate
    table (`cand_fqn`) from this column-level veto scan when `cand_fqn`
    ITSELF already has real table-level vocabulary evidence for `term`
    (the SAME confidence_tier HIGH/MEDIUM + column_name=="" evidence class
    match_dynamic_entity_terms uses to propose the candidate in the first
    place). Generic, reproducible gap found via a synthetic (non-the reference database)
    fixture: "Show patients with their appointments" grounds "Patient"
    first; when "Appointment" is then checked, its own natural table
    (main.Appointments, one FK hop from Patients) is scanned for column-
    level matches and its OWN identity column (AppointmentID) trivially
    token-matches the term "Appointment" -- which used to veto "Appointment"
    as a mere attribute of Patient, even though the term literally names
    that table's own entity. The original "Email"/"City" regressions this
    veto exists for (module docstring above) are a DIFFERENT shape: the
    column match happens on a table that has NO table-level evidence for
    the term at all (dbo.Emails is not itself a real match for "email" as
    an entity in those cases -- the veto is right there). This exclusion
    only removes the false-positive case where the candidate's own natural
    table is exactly the one being scanned; every other related table is
    still scanned unchanged, so the original regression this veto fixes
    stays fixed."""
    from data.business_knowledge_service import get_table_business_context
    from data.query_planning_service import _AUTO_SELECT_MIN_CONFIDENCE, _score_term_match_single

    def _has_own_table_level_evidence(fqn: str) -> bool:
        try:
            from data.vocabulary_bootstrap_service import get_generated_vocabulary

            vocabulary = get_generated_vocabulary(source_id, session=session)
        except Exception:  # noqa: BLE001
            return False
        term_lower = (term or "").lower().strip()
        if not term_lower:
            return False
        rows = vocabulary.get(term_lower) or []
        return any(
            row.get("confidence_tier") in ("HIGH", "MEDIUM")
            and not row.get("column_name")
            and row.get("table_fqn") == fqn
            for row in rows
        )

    # Deliberately _score_term_match_single (plain lexical/Jaccard match on
    # the term's own literal wording), NOT _score_term_match (which also
    # tries every governed SYNONYM expansion via expand_concept) — that
    # broader net is right for scoring candidates already narrowed to a
    # relevant pool, but too permissive here: live-confirmed against real
    # the reference database that "Program" expands to include the general-English synonym
    # "project", which then exact-matches an unrelated dbo.LMS_ShortList_
    # Results.Project column on one of dbo.EDU_Student's own trusted related
    # tables — vetoing a genuine second entity ("program", correctly meaning
    # dbo.EDU_Track) as if it were merely an attribute of Student. This
    # veto's whole purpose is catching a term's OWN literal column-level
    # evidence, not every synonym's.
    try:
        checked: set[str] = set()
        for table_fqn in table_fqns:
            ctx = get_table_business_context(source_id, user_id, table_fqn, session=session)
            if not ctx:
                continue
            relationships = ctx.get("relationships") or {}
            related = {
                r.get("to_table_fqn") or r.get("from_table_fqn")
                for r in [*(relationships.get("outbound") or []), *(relationships.get("inbound") or [])]
            }
            related.discard(None)
            for cand_fqn in {table_fqn, *related}:
                if cand_fqn in checked:
                    continue
                checked.add(cand_fqn)
                if _has_own_table_level_evidence(cand_fqn):
                    # cand_fqn is itself a real table-level match for `term`
                    # (e.g. main.Appointments for "Appointment") -- a column
                    # match here (its own identity column) is evidence the
                    # term names THIS table's own entity, not an attribute
                    # of whatever already-grounded entity led the scan here.
                    continue
                cand_ctx = ctx if cand_fqn == table_fqn else get_table_business_context(
                    source_id, user_id, cand_fqn, session=session,
                )
                if not cand_ctx:
                    continue
                # Persistence Consistency milestone, Part 1/2/12 -- a
                # foreign-key column on cand_fqn (e.g. Appointments.
                # PatientID) that happens to lexically match `term`
                # ("Patient") is NOT evidence `term` names a mere attribute
                # of cand_fqn -- it is a RELATIONSHIP POINTER to another
                # table, and when THAT target table itself has real
                # table-level evidence for the term (main.Patients does),
                # the column match is actually proof `term` names a real,
                # DIFFERENT entity cand_fqn merely references, not one of
                # cand_fqn's own attributes. Without this exclusion, "Which
                # patients have the most appointments?" grounds
                # "Appointment" first (a genuine second entity), then
                # "Patient" gets vetoed here purely because Appointments.
                # PatientID exists -- collapsing a real two-entity ranking
                # question down to one grounded entity and silently
                # degrading the eventual SQL to an ungrouped bare count
                # (live-confirmed regression on a generic, non-the reference database
                # fixture). Only skips columns whose FK target independently
                # already qualifies as the term's own table -- an ordinary
                # scalar attribute match is completely unaffected.
                fk_targets_by_column = {
                    r["from_column"]: r.get("to_table_fqn")
                    for r in (cand_ctx.get("relationships") or {}).get("outbound") or []
                    if r.get("from_column") and r.get("to_table_fqn")
                }
                for col in cand_ctx.get("columns") or []:
                    fk_target = fk_targets_by_column.get(col["column_name"])
                    if fk_target and _has_own_table_level_evidence(fk_target):
                        continue
                    dic = col.get("dictionary") or {}
                    if _score_term_match_single(term, col["column_name"], dic.get("business_label")) >= _AUTO_SELECT_MIN_CONFIDENCE:
                        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def apply_grounding(
    source_id: int, user_id: str, question: str,
    concepts: list[str], measure_terms: list[str], dimension_terms: list[str],
    *, ai_entity_hints: list[str] | None = None, session=None,
) -> dict:
    """Task 3/5 planner-facing orchestration. Matches the question against
    the fixed entity taxonomy AND, additively, against dynamic candidates
    with real per-source vocabulary evidence (match_dynamic_entity_terms —
    see its own docstring; this is what lets "instructor" ground even
    though it has no ENTITY_TAXONOMY entry), and loads each matched
    entity's contract (a cache hit when fresh — see get_or_build_entity_
    contract; this is the query-time read path, not a bulk rebuild). Real
    the reference database discovery showed grounding is often PARTIAL across a multi-entity
    question (e.g. only one of "students"/"enrollments"/"course" may
    resolve confidently), so this never gates all-or-nothing: callers get
    back whichever subset grounded, plus an explicit fully_grounded flag
    for the cases where a full broad-search skip is safe. A dynamic
    candidate that fails to independently confirm a canonical table is
    silently dropped rather than added to `unsupported` — it was only ever
    a hypothesis, not a term the fixed taxonomy positively named as an
    entity, so a failed guess must never trigger the unsupported-entity
    fast-exit path.

    ai_entity_hints (AI-Native Phase 2, Stage A v2) — entity names the AI
    question interpreter (core.semantic.ai_interpreter) named for a
    question it identified as a genuine multi-entity relationship
    (relationship_intent in list_with_related/scoped_count/
    scoped_aggregate; the caller's own responsibility to filter — this
    function accepts whatever list it's given). Treated EXACTLY like a
    match_dynamic_entity_terms candidate: appended to that list, subject to
    the identical get_or_build_entity_contract confirmation gate before it
    can ever become "grounded" — an unsupported hint is silently dropped,
    never trusted on the model's word alone. The one difference from a
    vocabulary-derived dynamic candidate is the entry point: a
    match_dynamic_entity_terms candidate must already appear as a split
    single-word deterministic term with qualifying table-level vocabulary
    evidence (structurally unable to represent a multi-word business
    concept like "user account" when extract_terms split it into two
    disconnected words "user"/"accounts", neither of which alone may carry
    that evidence), whereas an AI hint is handed here as the ALREADY-
    COMBINED phrase the AI recognized as one entity, so entity_synonyms'
    generic singular/plural fallback and get_or_build_entity_contract's own
    real per-source discovery search get a fair shot at it as a single
    concept. Deliberately kept OUT of concepts/measure_terms/
    dimension_terms/all_terms — Stage A (v1) merged an AI entity straight
    into concepts and live-confirmed that also fed the unrelated legacy
    raw-term fallback scorer (_score_table_authority in
    data.query_planning_service) whenever grounding didn't fully succeed
    for some other reason, shifting an unrelated question's own table
    selection (REG-R-02) without fixing the target case. Isolating the
    hint to this dynamic-candidate list only gives it a path through
    get_or_build_entity_contract's real confirmation gate — it has no path
    into any raw-term-scoring consumer of concepts/all_terms.

    Returns:
      {"matched_entities": [...],
       "grounded": {entity_name: contract_dict, ...},   # RESOLVED/PARTIAL with a canonical table
       "fully_grounded": bool,                           # every matched entity is grounded
       "seed_table_fqns": set[str],                       # canonical + preferred_view of every grounded entity
       "entity_table_map": {entity_name: canonical_table_fqn, ...},  # Day 2C Task 4 — per-entity, unlike seed_table_fqns
       "entity_trusted_tables": {entity_name: {canonical, *alternates}, ...},  # Day 2C Task 4 — per-entity role-selection scope
       "status_hints": {table_fqn: {"column_name","data_type","verified_values"}}}
    """
    matched = match_entities_for_terms(question, concepts, measure_terms, dimension_terms)

    # Part 2 — dynamic candidates beyond the fixed taxonomy (see match_
    # dynamic_entity_terms's own docstring). Appended after the fixed
    # matches so first-appearance ordering for the fixed set is unchanged;
    # a dynamic candidate only ever ENTERS matched_entities below once its
    # own contract confirms a real canonical table.
    dynamic_candidates = match_dynamic_entity_terms(
        source_id, concepts, measure_terms, dimension_terms, matched, session=session,
    )

    # AI-Native Phase 2, Stage A v2 — see ai_entity_hints's own docstring
    # note above. Appended after both the fixed and vocabulary-derived
    # dynamic candidates so their first-appearance ordering is unchanged;
    # deduplicated case-insensitively against both (an AI hint that merely
    # restates an entity already found is a no-op, not a second attempt).
    if ai_entity_hints:
        already_lower = {e.lower() for e in (*matched, *dynamic_candidates)}
        for hint in ai_entity_hints:
            hint_norm = (hint or "").strip()
            if hint_norm and hint_norm.lower() not in already_lower:
                dynamic_candidates.append(hint_norm.title())
                already_lower.add(hint_norm.lower())

    grounded: dict[str, dict] = {}
    unsupported: dict[str, dict] = {}
    seed_table_fqns: set[str] = set()
    entity_table_map: dict[str, str] = {}
    entity_trusted_tables: dict[str, set[str]] = {}
    status_hints: dict[str, dict] = {}
    confirmed_dynamic: list[str] = []

    def _ground(entity_name: str, contract: dict) -> None:
        grounded[entity_name] = contract
        seed_table_fqns.add(contract["canonical_table_fqn"])
        # Day 2C, Task 4 — role-selection control needs the per-entity
        # mapping preserved (seed_table_fqns below flattens every
        # grounded entity's tables into one undifferentiated set, which
        # is exactly what let a partially-grounded question's broad
        # search outscore a grounded entity's own canonical table for
        # its own role; see query_planning_service._narrow_to_grounded_
        # table, the consumer of these two fields). entity_trusted_
        # tables includes this entity's OWN alternate_candidates too —
        # Day 2A Task 4's bounded status-retry (below) needs a real
        # second candidate for the SAME entity to retry into, so
        # role-selection narrowing must not collapse to the canonical
        # table alone (confirmed by a real regression against
        # tests/test_status_resolution_bounded_retry.py).
        entity_table_map[entity_name] = contract["canonical_table_fqn"]
        alternate_candidates = contract["contract"].get("alternate_candidates") or []
        entity_trusted_tables[entity_name] = {contract["canonical_table_fqn"], *alternate_candidates}
        if contract["preferred_view_fqn"]:
            seed_table_fqns.add(contract["preferred_view_fqn"])
        # Day 2A Task 4's bounded status-retry needs at least one real
        # alternative to retry against — without this, grounding would
        # narrow the query-time candidate pool to the single canonical
        # table and silently defeat that mechanism whenever the
        # canonical table itself lacks a compatible status field.
        seed_table_fqns.update(alternate_candidates)
        status = contract["contract"].get("status_semantics")
        if status and status.get("verified_values"):
            status_hints[status["table_fqn"]] = {
                "column_name": status["column_name"], "data_type": status["data_type"],
                "verified_values": status["verified_values"],
            }
        if entity_name not in matched:
            confirmed_dynamic.append(entity_name)

    # pending is populated by both Pass 1 (below) and Pass 2 (further down)
    # so Pass 3/4/5's relationship-based disambiguation considers a
    # genuinely-tied fixed-taxonomy entity exactly the same as a genuinely-
    # tied dynamic one — previously fixed entities had no path into pending
    # at all, so a fixed entity's own close-margin ambiguity (e.g.
    # "Enrollment", real evidence, top candidate 0.8489 vs runner-up 0.8474
    # — a razor-thin 0.0015 margin, live-confirmed against a real production database) could
    # never benefit from a trusted relationship path to another grounded
    # entity, or corroborate/be corroborated by another still-pending term,
    # the way a dynamic candidate always could.
    pending: dict[str, list[dict]] = {}

    # Pass 1 — fixed-taxonomy entities: ground outright, record a confirmed
    # dead end as unsupported, or (Contextual Ambiguity Resolution) queue a
    # genuine close-margin tie into `pending` for Pass 3/4/5 instead of
    # silently dropping it.
    for entity_name in matched:
        contract = get_or_build_entity_contract(source_id, user_id, entity_name, session=session)
        if contract["resolution_status"] in ("RESOLVED", "PARTIAL") and contract["canonical_table_fqn"]:
            _ground(entity_name, contract)
            continue
        if (
            contract["resolution_status"] in UNSUPPORTED_CONTRACT_STATUSES
            and _is_confirmed_dead_end(contract)
            and not _has_validated_alternative_evidence(entity_name, source_id, session=session)
        ):
            # Day 2D, Priority 2 / Day 2E, Task 4 — a matched entity whose
            # contract reflects a COMPLETED discovery attempt (deterministic,
            # plus AI adjudication when enabled) with no usable canonical
            # table, for the CURRENT metadata_revision (get_or_build_entity_
            # contract's own cache freshness check, unchanged) — see
            # UNSUPPORTED_CONTRACT_STATUSES', _is_confirmed_dead_end's, and
            # _has_validated_alternative_evidence's own docstrings for the
            # two independent carve-outs: a NO_CANDIDATE whose top candidate
            # cleared the auto-select confidence floor and lost only on
            # margin against a real, close runner-up is a genuine, answerable
            # ambiguity, not a dead end; and a zero-confidence NO_CANDIDATE
            # that nonetheless has an APPROVED concept mapping, or generated-
            # vocabulary evidence pointing at an APPROVED table (evidence the
            # entity-contract's own narrower discovery never considers), is
            # not confirmed unsupported either — both deliberately fall
            # through here, unclassified, to keep flowing to the ordinary
            # resolution/clarification path. The original resolution_status
            # stays on `contract` unchanged either way, for diagnostics.
            # Surfaced separately from "just didn't match anything" so a
            # caller can fast-exit a question that names ONLY confirmed-
            # unsupported entities without repeating the broad search/AI
            # interpretation this same contract already proved futile — see
            # query_planning_service.
            # plan_business_query's own fast-exit block and
            # grounding_fully_accounted() below, the two consumers of this
            # field.
            unsupported[entity_name] = contract
            continue
        if contract["resolution_status"] != "NO_CANDIDATE":
            continue  # an in-progress/unrecognized status — leave exactly as before, never guess
        tied = _tied_top_candidates(source_id, user_id, entity_name, session=session)
        if tied is not None:
            pending[entity_name] = tied  # genuine close-margin tie — retry in Pass 3/4/5

    # Pass 2 — dynamic candidates: ground outright, or try Part 3/5's
    # relationship tie-break against whatever's grounded so far (fixed
    # entities from Pass 1, plus any dynamic candidate already grounded
    # earlier in this same pass).
    #
    # Contextual Ambiguity Resolution — rescue is tried BEFORE the local
    # tie-break here (previously only reached in Pass 5, and only for
    # entities that never even tied locally). Live-confirmed against real
    # the reference database why order matters: _relationship_disambiguate only ever chooses
    # AMONG whatever gather_candidate_evidence's bounded LOCAL pool already
    # retrieved for this term — for "program" that pool is a dozen
    # Internship/Mentorship-Program tables sharing one coarse
    # entity_assignment classification (the reason they tie in the first
    # place), and dbo.EDU_Track (whose only evidence is a precise,
    # term-specific ProgramType column) never scores high enough to even
    # enter that tied set. _relationship_disambiguate can therefore only
    # ever confirm the tied pool's own top scorer — it has no way to
    # discover a materially better candidate sitting outside the pool it
    # was given, and used to run first, locking that answer in before
    # Pass 5's wider check ever got a chance.
    # _relationship_rescued_candidate searches the FULL source-wide
    # vocabulary-evidenced table set (not just the locally tied few) and
    # only fires when EXACTLY ONE such table is connected — a strictly
    # more complete evidence pool with an equally strict (indeed stricter,
    # since it competes against every vocabulary-evidenced table on the
    # source, not just 2-3 locally tied ones) uniqueness bar. Trying it
    # first costs nothing when it doesn't apply (returns None immediately,
    # falling through to the unchanged tie-break exactly as before), and
    # correctly wins when it does: it is evidence the LOCAL pool that
    # produced the tie was itself incomplete, not that the tie-break's
    # answer was wrong on its own terms.
    for entity_name in dynamic_candidates:
        # General Attribute Resolution — a term that already scores as a
        # real column on an entity THIS question already grounded (or its
        # own trusted related tables) is evidence it names an ATTRIBUTE,
        # not a second entity; never even attempt to promote it here, so
        # it stays available for the ordinary/related-attribute resolution
        # path downstream instead of hijacking relationship-plan routing.
        if entity_table_map and _term_matches_grounded_table_column(
            source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
        ):
            continue
        contract = get_or_build_entity_contract(source_id, user_id, entity_name, session=session)
        if contract["resolution_status"] in ("RESOLVED", "PARTIAL") and contract["canonical_table_fqn"]:
            _ground(entity_name, contract)
            continue
        if contract["resolution_status"] != "NO_CANDIDATE":
            continue  # a confirmed dead end for a dynamic hypothesis is silently dropped, never "unsupported"
        if entity_table_map:
            rescued = _relationship_rescued_candidate(
                source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
            )
            if rescued is not None:
                _ground(entity_name, rescued)
                continue
            tiebreak = _relationship_disambiguate(
                source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
            )
            if tiebreak is not None:
                _ground(entity_name, tiebreak)
                continue
        tied = _tied_top_candidates(source_id, user_id, entity_name, session=session)
        if tied is not None:
            pending[entity_name] = tied  # genuinely ambiguous — retry in Pass 3/4/5

    # Pass 3 — anchor propagation: a pending candidate resolved via Pass 4
    # (or a later Pass 3 iteration) becomes a fresh anchor for any OTHER
    # still-pending candidate. Bounded by len(pending) iterations (at most
    # _MAX_RELATIONSHIP_ENTITIES-ish in practice), so this always
    # terminates even in a worst-case all-pending question.
    progressed = True
    while progressed and pending:
        progressed = False
        for entity_name in list(pending.keys()):
            if not entity_table_map:
                break
            # Same rescue-before-tiebreak ordering as Pass 2, for the same
            # reason: rescue checks the full vocabulary-evidenced pool, not
            # just this entity's own locally-tied candidates, so it can
            # find a materially better answer the local tie never included.
            resolved = _relationship_rescued_candidate(
                source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
            )
            if resolved is None:
                resolved = _relationship_disambiguate(
                    source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
                )
            if resolved is not None:
                _ground(entity_name, resolved)
                del pending[entity_name]
                progressed = True

    # Pass 4 — Part 3, mutual Joint Entity Resolution: two candidates that
    # are BOTH still pending (neither has any other already-grounded entity
    # to corroborate against) may corroborate EACH OTHER. Tried for every
    # remaining pair; a pair that resolves grounds both and feeds Pass 3
    # again for anything left.
    pending_names = list(pending.keys())
    for i in range(len(pending_names)):
        for j in range(i + 1, len(pending_names)):
            a, b = pending_names[i], pending_names[j]
            if a not in pending or b not in pending:
                continue  # already resolved by an earlier pair this pass
            resolved = _joint_disambiguate_pair(
                source_id, user_id, a, pending[a], b, pending[b], session=session,
            )
            if resolved is not None:
                contract_a, contract_b = resolved
                _ground(a, contract_a)
                _ground(b, contract_b)
                del pending[a]
                del pending[b]

    # Pass 5 — relationship rescue for anything STILL pending. A term whose
    # own tied lexical candidates (e.g. several unrelated tables that merely
    # share its word via a coarse entity_assignment) include none with a
    # trusted path to a grounded entity never reaches _relationship_
    # rescued_candidate in Pass 2 at all (Pass 2 only tries it when there
    # was NO tie to record in the first place) and would otherwise be
    # silently dropped here, unresolved forever, even though the rescue
    # searches the FULL vocabulary-evidenced table set for this term — not
    # just the ones that happened to tie lexically — and may find a
    # genuinely connected table the tied set never included. Confirmed live:
    # "program"'s own tied candidates are a dozen unrelated Internship/
    # Mentorship-program tables with zero relationship to Student, while the
    # real match (dbo.EDU_Track) never scored high enough to tie at all.
    for entity_name in list(pending.keys()):
        if not entity_table_map:
            break
        rescued = _relationship_rescued_candidate(
            source_id, user_id, entity_name, set(entity_table_map.values()), session=session,
        )
        if rescued is not None:
            _ground(entity_name, rescued)
            del pending[entity_name]

    matched_entities = [*matched, *confirmed_dynamic]

    # Specialized Concept Principle — see _entity_match_has_unconsumed_
    # modifier's own docstring above. Only checked for entities that DID
    # ground (an unsupported/pending entity already keeps fully_grounded
    # False via the len(grounded) == len(matched_entities) check below,
    # regardless of this), and only costs anything when matched_entities is
    # non-empty in the first place.
    all_question_terms = [*concepts, *measure_terms, *dimension_terms]
    has_unconsumed_modifier = any(
        _entity_match_has_unconsumed_modifier(entity_name, question, all_question_terms)
        for entity_name in grounded
    )
    return {
        "matched_entities": matched_entities,
        "grounded": grounded,
        "unsupported": unsupported,
        "fully_grounded": (
            bool(matched_entities)
            and len(grounded) == len(matched_entities)
            and not has_unconsumed_modifier
        ),
        "seed_table_fqns": seed_table_fqns,
        "entity_table_map": entity_table_map,
        "entity_trusted_tables": entity_trusted_tables,
        "status_hints": status_hints,
    }


def grounding_fully_accounted(grounding: dict | None) -> bool:
    """Day 2E, Task 3 — true when every entity this question named is
    either grounded (a verified contract with a canonical table) OR
    confirmed-unsupported (Task 4's negative-contract bucket): nothing left
    that a live AI pass or a broad candidate search could actually help
    resolve. Stricter than apply_grounding's own `fully_grounded` (which
    only counts positively-grounded entities) — this is what should gate
    any live AI/broad-search detour, not just the narrower "everything
    resolved" case: a confirmed dead end is just as settled as a verified
    contract, for the purpose of deciding whether more search could help.
    """
    if not grounding or not grounding.get("matched_entities"):
        return False
    grounded = grounding.get("grounded") or {}
    unsupported = grounding.get("unsupported") or {}
    return all(e in grounded or e in unsupported for e in grounding["matched_entities"])


def generate_all_entity_contracts(source_id: int, user_id: str) -> dict:
    """Warms (or refreshes) every target entity's contract for one source.
    The lazy per-entity path inside get_or_build_entity_contract is the
    real, general mechanism this just exercises up front — useful for
    pre-warming before a verification pass, never required for correctness."""
    results: dict[str, dict] = {}
    for entity_name in TARGET_ENTITIES:
        results[entity_name] = get_or_build_entity_contract(source_id, user_id, entity_name)
    return results


def current_metadata_revision(source_id: int, *, session=None) -> str:
    """Cheap fingerprint of the metadata state a contract was built from —
    Task 6's cache-invalidation key. Reuses the same latest-schema-snapshot
    / latest-profiling-snapshot lookups business_knowledge_service.py
    already performs per table-context fetch."""
    from data.profiling_snapshot_resolver import get_latest_profiling_snapshot

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        schema_row = conn.execute(
            "SELECT id FROM schema_snapshots WHERE source_id = ? ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
        schema_snap_id = schema_row["id"] if schema_row else None
        prof_snap = get_latest_profiling_snapshot(source_id, conn=conn)
        prof_snap_id = prof_snap.id if prof_snap else None
    finally:
        if own_connection:
            conn.close()
    return f"{schema_snap_id}:{prof_snap_id}"
