"""
Sprint 1 — AI Brain candidate-table retrieval.

Drop-in replacement for data.query_planning_service._collect_candidate_tables()
at the call site data/query_planning_service.py:1111. Question Understanding
(extract_terms/extract_query_intent, called by the caller) -> Business Domain
Ranking -> search_metadata() table ranking -> 1-hop relationship expansion ->
candidate_tables. Every stage reuses an existing service; nothing here builds
a new SQL engine, metadata engine, or discovery engine.

Domain narrowing is advisory only (_select_domain_filter): it is applied to
search_metadata() only when the top domain is both confident and unambiguous.
Any empty or failed result returns an empty set. As of the Semantic Retrieval
Integration change, the caller (plan_business_query) no longer falls back to
the unbounded _collect_candidate_tables() scan on an empty set — it treats
"no candidates" as a safe, natural unresolved/clarification outcome instead.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from data.db import get_connection
from data.request_metadata_session import MetadataSearchFailedError
from data.search_service import search_metadata
from data.vocabulary_service import expand_concept, normalize_term
from data.concept_mapping_service import get_all_approved_mappings, get_synonym_canonical
from data.vocabulary_bootstrap_service import get_generated_vocabulary
from core.domains.rules import _DOMAIN_KEYWORDS, _CONFIDENCE_DENOMINATOR, _tokenize, _hits

logger = logging.getLogger(__name__)

# Domain filter is applied only when the top domain clears both gates —
# narrowing on a weak/ambiguous signal risks silently excluding relevant
# tables, so an unclear question searches unfiltered instead.
_DOMAIN_CONFIDENCE_MIN = 0.70
_DOMAIN_MARGIN_MIN = 0.15

# Phase 2, Step 7 — an approved concept_table_mappings row's synthetic
# relevance_score, set well above the realistic range of _search_all's own
# scores (observed up to ~400 on real CCPP data) so the _RETRIEVAL_TABLE_LIMIT
# cap never excludes it, mirroring how a genuinely top-scored table is never
# excluded either. Never used to bypass query_planning_service's own
# resolution gate — only to guarantee the table enters the candidate set.
_APPROVED_MAPPING_SCORE_FLOOR = 1_000_000.0

# Enterprise Phase 4 — generated_business_vocabulary additive merge floors.
# Both sit below _APPROVED_MAPPING_SCORE_FLOOR (so a human-approved mapping
# always outranks auto-derived vocabulary for the same term) and above
# _search_all's realistic ceiling (so a candidate is never crowded out of
# the _RETRIEVAL_TABLE_LIMIT cap by that ceiling alone). This floor only
# controls survival into the candidate set — it does NOT decide auto-select
# vs. ambiguity; that is entirely query_planning_service's
# _generated_vocabulary_bonus (a small, tier-sized score bonus), so two
# competing MEDIUM-tier tables both surviving here can still correctly fall
# through to the existing clarification gate. LOW-tier rows are never merged.
_GENERATED_VOCAB_HIGH_FLOOR = 500_000.0
_GENERATED_VOCAB_MEDIUM_FLOOR = 50_000.0

# Same "one broad term can't fill the whole cap" discipline as
# _PER_TERM_LIMIT below, applied to generated vocabulary specifically: a
# term with many competing generated candidates (e.g. "placements" matching
# a dozen loosely-related legacy tables) must not, by itself, consume the
# entire _RETRIEVAL_TABLE_LIMIT and crowd out a table a DIFFERENT term in
# the same question needs — reproduced against real CCPP data, where an
# unbounded merge regressed 3 previously-passing multi-term questions
# ("Lowest placement fee", "Average time to fill per job order", "Which
# recruiter has the most placements?") by displacing the specific table
# their other term actually needed. Rows are already ordered by
# confidence_score DESC per term (get_generated_vocabulary's own contract),
# so slicing keeps the strongest candidates.
_GENERATED_VOCAB_PER_TERM_LIMIT = 5

# Bounded per the Sprint 1 scaling requirement. Not search_metadata's own
# internal _MAX_CANDIDATES pre-scoring cap, which is untouched.
_RETRIEVAL_TABLE_LIMIT = 25
_RELATIONSHIP_EXPANSION_CAP = 10

# Sprint 1.3 — candidate diversity (Problem 1): per-term top-N so one broad
# term (e.g. "students", matching dozens of tables) can't fill the entire
# _RETRIEVAL_TABLE_LIMIT and crowd out a table another term in the same
# question needs (e.g. "homework"). Applied additively on top of the
# existing combined-question search, not instead of it.
_PER_TERM_LIMIT = 5

# Sprint 1.3 — advisory domain rescue (Problem 2): when a confident domain
# filter is active, also try each term unfiltered so a clearly-matching
# table excluded only by a domain-assignment gap (not by relevance) isn't
# made invisible. Capped, and merged only when "high relevance" relative to
# that term's own best unfiltered hit — see _RESCUE_RELEVANCE_RATIO.
_RESCUE_LIMIT = 5
_RESCUE_RELEVANCE_RATIO = 0.5


def _rank_domains(question: str, terms: list[str]) -> list[dict]:
    """
    Score question text against the existing table-domain keyword map.
    Reuses core.domains.rules' tokenizer, keyword map, and confidence scale
    (the same one detect_table_domain() uses for table names) — applied to
    question/term tokens instead. No new taxonomy.
    """
    tokens = set(_tokenize(question or ""))
    for term in terms:
        tokens.update(_tokenize(term))
    tokens_list = list(tokens)

    raw: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        hits = [kw for kw in keywords if _hits(kw, tokens_list)]
        if hits:
            raw[domain] = float(len(hits))
            matched[domain] = hits

    ranked = [
        {
            "domain": d,
            "confidence": round(min(1.0, score / _CONFIDENCE_DENOMINATOR), 3),
            "matched_keywords": matched[d],
        }
        for d, score in raw.items()
    ]
    ranked.sort(key=lambda r: -r["confidence"])
    return ranked


def _select_domain_filter(question: str, terms: list[str]) -> str | None:
    """
    Advisory-only domain narrowing. Applies the domain filter to
    search_metadata only when the top domain is both confident
    (>= _DOMAIN_CONFIDENCE_MIN) and unambiguous (margin over the runner-up
    >= _DOMAIN_MARGIN_MIN); otherwise returns None so search runs unfiltered.
    """
    ranked = _rank_domains(question, terms)
    if not ranked:
        return None
    top = ranked[0]["confidence"]
    if top < _DOMAIN_CONFIDENCE_MIN:
        return None
    runner_up = ranked[1]["confidence"] if len(ranked) > 1 else 0.0
    if (top - runner_up) < _DOMAIN_MARGIN_MIN:
        return None
    return ranked[0]["domain"]


def _search_tables(query_text: str, source_id: int, domain: str | None, *, session=None) -> list[dict]:
    result = search_metadata(
        q=query_text, source_id=source_id, asset_type="table",
        domain=domain, limit=_RETRIEVAL_TABLE_LIMIT, session=session,
    )
    return result.get("results") or []


def _search_columns_as_tables(query_text: str, source_id: int, domain: str | None, *, session=None) -> list[dict]:
    """
    Column-level matches folded down to their owning table. Most
    measure/dimension terms ("revenue", "status") are column business
    labels/meanings, never table names or descriptions — _search_tables
    alone cannot surface the owning table for these, which
    find_business_assets(term=...) always could (it unions table AND
    column matches, knowledge_graph_service.py:346-351). This reuses
    search_metadata's own bounded, scored column branch (asset_type=
    "column") instead of a second unbounded query, and returns only the
    two fields _merge_best/the final cap actually read.
    """
    result = search_metadata(
        q=query_text, source_id=source_id, asset_type="column",
        domain=domain, limit=_RETRIEVAL_TABLE_LIMIT, session=session,
    )
    rows: list[dict] = []
    for r in result.get("results") or []:
        schema_name = r.get("schema_name") or ""
        table_name = r.get("table_name") or ""
        if not schema_name or not table_name:
            continue
        rows.append({
            "qualified_name": f"{schema_name}.{table_name}",
            "relevance_score": r["relevance_score"],
        })
    return rows


def _search_all(query_text: str, source_id: int, domain: str | None, *, session=None) -> list[dict]:
    """Table- and column-level matches combined and re-sorted by
    relevance_score, so callers that slice the top N (per-term diversity,
    domain rescue) get the true top N across both branches rather than
    every table match ahead of every column match regardless of score."""
    combined = (
        _search_tables(query_text, source_id, domain, session=session)
        + _search_columns_as_tables(query_text, source_id, domain, session=session)
    )
    combined.sort(key=lambda r: -r["relevance_score"])
    return combined


def _latest_snapshot_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


def _expand_relationships(source_id: int, table_fqns: set[str], *, session=None) -> set[str]:
    """
    1-hop declared-FK expansion, capped, no-op when no active snapshot
    exists. Mirrors the relationship_status IN ('AUTO','APPROVED') read
    pattern knowledge_graph_service._build_fk_graph() already uses.
    """
    if not table_fqns:
        return set()
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        if session is not None:
            snapshot_id = session.get_or_compute(
                f"latest_schema_snapshot_id:{source_id}",
                lambda: _latest_snapshot_id(conn, source_id),
            )
        else:
            snapshot_id = _latest_snapshot_id(conn, source_id)
        if not snapshot_id:
            return set()
        fqn_list = list(table_fqns)
        ph = ",".join("?" for _ in fqn_list)
        rows = conn.execute(
            f"""SELECT from_table_fqn, to_table_fqn FROM table_relationships
                WHERE source_id = ? AND snapshot_id = ?
                  AND relationship_status IN ('AUTO', 'APPROVED')
                  AND (from_table_fqn IN ({ph}) OR to_table_fqn IN ({ph}))""",
            (source_id, snapshot_id, *fqn_list, *fqn_list),
        ).fetchall()
        neighbors: set[str] = set()
        for r in rows:
            if r["from_table_fqn"] in table_fqns and r["to_table_fqn"] not in table_fqns:
                neighbors.add(r["to_table_fqn"])
            if r["to_table_fqn"] in table_fqns and r["from_table_fqn"] not in table_fqns:
                neighbors.add(r["from_table_fqn"])
        return set(list(neighbors)[:_RELATIONSHIP_EXPANSION_CAP])
    finally:
        if own_connection:
            conn.close()


def _merge_best(merged: dict[str, dict], results: list[dict]) -> None:
    """Merge *results* into *merged* (keyed by qualified_name), keeping the
    higher relevance_score on collision. Preserves each result dict as-is —
    scores, penalties, and every existing field are untouched."""
    for r in results:
        fqn = r.get("qualified_name")
        if not fqn:
            continue
        existing = merged.get(fqn)
        if existing is None or r["relevance_score"] > existing["relevance_score"]:
            merged[fqn] = r


def _is_column_name_only_evidence(row: dict) -> bool:
    """True when a generated_business_vocabulary row's *only* supporting
    evidence is a single column's name matching the term (evidence_type
    "column_name") — never table_name_token, dictionary_business_name,
    curated_synonym, or any other table-identity evidence, which pass
    through unaffected.

    That evidence says a COLUMN matches the term, not that the TABLE itself
    is about it — too weak to justify the merge's synthetic score floor
    (guaranteed candidate-pool survival above any real search score, by
    design). Reproduced against real CCPP data: a MEDIUM-tier "state"
    column_name row on dbo.ADF_BHCandidates (a job-candidates table that
    happens to also store a mailing-address State, same as any CRM
    location table would) tied exactly with the correct answer's own
    dictionary/table-identity-evidenced match, turning a previously
    unambiguous dimension resolution into a false "ambiguous_dimension"
    refusal. No table/term is special-cased here — the exclusion is by
    evidence shape alone.
    """
    try:
        evidence = json.loads(row.get("evidence_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(evidence) and all(e.get("type") == "column_name" for e in evidence)


def get_candidate_tables(
    source_id: int, user_id: str, question: str, terms: list[str], *, session=None,
) -> set[str]:
    """
    Sprint 1 AI Brain entry point — drop-in for _collect_candidate_tables().

    Thin wrapper over _retrieve() for backward compatibility: every existing
    caller/test expects a bare set(). Use get_candidate_tables_with_ranking()
    instead when the scored ranking is also needed (e.g. to present
    below-threshold candidates for clarification) — both share this same
    single retrieval implementation, so this is not a second pipeline.

    Returns an empty set on any degraded, failed, or no-match condition.
    The caller (plan_business_query) treats an empty set as "no candidates"
    and does not fall back to the unbounded _collect_candidate_tables() scan
    — an empty result flows through to a safe unresolved plan outcome
    instead. Never raises.
    """
    return get_candidate_tables_with_ranking(source_id, user_id, question, terms, session=session)[0]


def get_candidate_tables_with_ranking(
    source_id: int, user_id: str, question: str, terms: list[str], *, session=None,
) -> tuple[set[str], list[dict], list[dict]]:
    """
    Same retrieval as get_candidate_tables(), also returning the scored,
    capped ranking (table_fqn + relevance_score, best first) that produced
    it — previously computed internally and discarded. Intended for
    presenting "here's what was found but not confidently picked" evidence
    (e.g. clarification options), not for a second selection mechanism.

    The ranking list only ever contains genuinely search-scored tables — the
    1-hop relationship-expansion neighbors folded into the returned set
    have no relevance_score of their own and are deliberately left out of
    the ranking (they were never independently matched against the terms).

    Phase 3, Step 4 — third element: remembered_terminology, one structured
    {evidence_type, original_term, canonical_term, source} record per term
    whose search actually ran under a human-taught synonym instead of the
    literal term (see _remembered_terminology_evidence). Pure explainability
    — it carries no scores and never affects table_fqns/ranking above.

    Phase 3.2A — request-scoped dedup: query_planning_service calls this
    same (source_id, question, terms) combination from more than one place
    in a single planning request (the main resolution path and
    _find_sufficient_single_object's own candidate lookup) — with a
    session, the second call reuses the first's result instead of redoing
    the full retrieval (see RequestMetadataSession.get_or_compute_search).
    Without a session (legacy callers), behavior is unchanged: every call
    runs the full retrieval.
    """
    if session is None:
        return _retrieve(source_id, user_id, question, terms)
    key = ("candidate_tables_with_ranking", source_id, user_id, question, tuple(terms))
    return session.get_or_compute_search(
        key, lambda: _retrieve(source_id, user_id, question, terms, session=session)
    )


def _remembered_terminology_evidence(source_id: int, terms: list[str], *, session=None) -> list[dict]:
    """
    Phase 3, Step 4 — one evidence record per term for which a remembered
    synonym (concept_mapping_service.get_synonym_canonical) actually changed
    the term used for retrieval. Deliberately excludes:
      - an unknown term (get_synonym_canonical returns None);
      - the canonical term used directly (get_synonym_canonical returns None
        for it — nothing is taught for a term pointing at itself);
      - a self-referential mapping (same guard as remember_synonym's own
        no-op case, belt-and-suspenders since remember_synonym never stores
        one);
      - another source's mapping (get_synonym_canonical is already
        source_id-scoped).
    Deduplicated by (original_term, canonical_term) so a term repeated
    across measures/dimensions/concepts yields one record, not several.
    """
    evidence: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for t in terms:
        canonical = get_synonym_canonical(source_id, t, session=session)
        if not canonical or canonical == normalize_term(t):
            continue
        pair = (t, canonical)
        if pair in seen:
            continue
        seen.add(pair)
        evidence.append({
            "evidence_type": "remembered_terminology",
            "original_term": t,
            "canonical_term": canonical,
            "source": "user_memory",
        })
    return evidence


def _retrieve(
    source_id: int, user_id: str, question: str, terms: list[str], *, session=None,
) -> tuple[set[str], list[dict], list[dict]]:
    """Shared implementation behind get_candidate_tables() and
    get_candidate_tables_with_ranking() — see Sprint 1.3 docstring below for
    the retrieval stages. Returns (table_fqns, ranked, remembered_terminology)
    where ranked is the scored, capped list (before relationship expansion),
    table_fqns is ranked's qualified_names plus their 1-hop expansion
    neighbors, and remembered_terminology is explainability-only evidence
    (see _remembered_terminology_evidence) that never affects the other two.

    Sprint 1.3 adds two advisory, additive stages on top of the original
    combined-question search: per-term candidate diversity (Problem 1) and
    domain rescue (Problem 2). Neither replaces the base search or changes
    its scoring — both only add more candidates for the same final,
    score-sorted cap to choose from.

    Returns ({}, [], []) on any degraded, failed, or no-match condition. The
    caller (plan_business_query) treats an empty set as "no candidates" and
    does not fall back to the unbounded _collect_candidate_tables() scan —
    an empty result flows through to a safe unresolved plan outcome
    instead. Never raises.
    """
    if not terms:
        return set(), [], []
    try:
        # Phase 3, Step 2 — remembered-synonym resolution: consult the
        # human-taught concept_term_synonyms mapping (get_synonym_canonical)
        # BEFORE any fuzzy search runs, mirroring query_planning_service's
        # own substitution. A term with no remembered synonym passes through
        # unchanged, so this is a no-op (identical to today's behavior) for
        # any source with nothing taught, and safely idempotent if the
        # caller already substituted (a canonical term has no synonym of its
        # own to resolve).
        # Phase 3, Step 4 — captured once, alongside the substitution, so
        # the exact terms that changed are recorded without a second lookup
        # pass or any change to the substitution result itself.
        remembered_terminology = _remembered_terminology_evidence(source_id, terms, session=session)
        terms = [get_synonym_canonical(source_id, t, session=session) or t for t in terms]

        query_text = " ".join(terms)
        domain = _select_domain_filter(question, terms)

        # Base: the original combined-question search, plus its column-level
        # half (_search_all) — most measure/dimension terms only exist as
        # column business labels, never table names.
        ranked = _search_all(query_text, source_id, domain, session=session)
        if not ranked and domain is not None:
            # A narrow domain filter must never be the sole reason for zero
            # results — retry once unfiltered before signalling fallback.
            ranked = _search_all(query_text, source_id, None, session=session)

        # Vocabulary-expansion fallback — only when the literal terms found
        # NOTHING at all (not even a domain-unfiltered retry). This is
        # deliberately a last resort, not blended into every search: adding
        # a term's normalized/synonym forms (vocabulary_service.
        # expand_concept(), e.g. "clients" -> "client") to a search that's
        # already finding matches would let unrelated tables that merely
        # share a common expanded word (e.g. "student") earn an extra
        # exact-tier score contribution, inflating their rank past a
        # correct, narrower match — this regressed
        # test_broad_plural_matches_do_not_consume_whole_candidate_budget
        # when tried. Gating on "raw search found nothing" avoids that
        # entirely: expansion only ever fires when there is no existing
        # ranking to distort, which is also precisely the "Unresolved
        # term(s) cannot be planned" failure mode this closes. Each
        # original term is kept alongside its expansion (expand_concept()
        # returns the *normalized* form, not the raw term, so both must be
        # searched) and domain classification stays on the literal
        # terms/question throughout — only retrieval ever expands.
        if not ranked:
            expanded_terms = list(dict.fromkeys(
                t for term in terms for t in ([term] + (expand_concept(term) or []))
            ))
            if expanded_terms != terms:
                expanded_query_text = " ".join(expanded_terms)
                ranked = _search_all(expanded_query_text, source_id, domain, session=session)
                if not ranked and domain is not None:
                    ranked = _search_all(expanded_query_text, source_id, None, session=session)

        merged: dict[str, dict] = {}
        _merge_best(merged, ranked)

        # Candidate diversity (Problem 1) — only meaningful with more than
        # one term; a single-term question is already fully covered by the
        # base search above, so skip the extra queries entirely.
        if len(terms) > 1:
            for term in terms:
                per_term = _search_all(term, source_id, domain, session=session)[:_PER_TERM_LIMIT]
                _merge_best(merged, per_term)

        # Domain rescue (Problem 2) — only relevant when a confident domain
        # filter is actually narrowing the search; regardless of term count,
        # since the blind spot (a real table excluded by a domain-assignment
        # gap) can affect a single-term question just as much as a multi-term
        # one.
        if domain is not None:
            for term in terms:
                rescue = _search_all(term, source_id, None, session=session)[:_RESCUE_LIMIT]
                if not rescue:
                    continue
                top_rescue_score = rescue[0]["relevance_score"]
                threshold = top_rescue_score * _RESCUE_RELEVANCE_RATIO
                high_relevance = [r for r in rescue if r["relevance_score"] >= threshold]
                _merge_best(merged, high_relevance)

        # Phase 2, Step 7 — approved scoped semantic memory (inclusion, not
        # selection). One more additive merge source, structurally identical
        # to the diversity/rescue stages above: an APPROVED (AUTO_APPROVED/
        # HUMAN_APPROVED only — GENERATED/SUGGESTED rows are never read here)
        # concept_table_mappings row for one of this question's terms
        # guarantees that table ENTERS the candidate set, with a synthetic
        # score set above the realistic top of _search_all's range so the
        # _RETRIEVAL_TABLE_LIMIT cap never crowds it out — exactly like a
        # genuinely top-scored table never getting excluded. This does not
        # select the final answer by itself; query_planning_service's own
        # _concept_mapping_bonus additionally has to clear the same
        # _AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN gate every other
        # candidate goes through.
        # One bulk read for the whole source (not one per term) — avoids N
        # DB round trips for what is, on most sources, an empty table.
        approved_by_term = get_all_approved_mappings(source_id, session=session)
        if approved_by_term:
            for term in terms:
                for mapping in approved_by_term.get(normalize_term(term), []):
                    _merge_best(merged, [{
                        "qualified_name": mapping["table_fqn"],
                        "relevance_score": _APPROVED_MAPPING_SCORE_FLOOR + mapping.get("confidence", 0.0),
                    }])

        # Enterprise Phase 4 — generated_business_vocabulary additive merge
        # (inclusion, not selection), structurally identical to the approved-
        # mapping merge just above but at a lower floor so it never outranks
        # an approved mapping for the same term. One bulk read for the whole
        # source. LOW-tier rows are auto-derived guesses this module's own
        # docstring says must never be silently used — excluded here, as is
        # any row whose only evidence is a column-name match rather than the
        # table's own identity (see _is_column_name_only_evidence).
        generated_by_term = get_generated_vocabulary(source_id, session=session)
        if generated_by_term:
            for term in terms:
                candidates = [
                    row for row in generated_by_term.get(normalize_term(term), [])
                    if row.get("confidence_tier") != "LOW"
                    and not _is_column_name_only_evidence(row)
                ][:_GENERATED_VOCAB_PER_TERM_LIMIT]
                for row in candidates:
                    tier = row.get("confidence_tier")
                    floor = _GENERATED_VOCAB_HIGH_FLOOR if tier == "HIGH" else _GENERATED_VOCAB_MEDIUM_FLOOR
                    _merge_best(merged, [{
                        "qualified_name": row["table_fqn"],
                        "relevance_score": floor + row.get("confidence_score", 0.0),
                    }])

        if not merged:
            return set(), [], remembered_terminology

        # Merge/dedupe done — apply the existing total candidate cap last,
        # preserving relevance-score ordering (and every penalty already
        # baked into those scores).
        capped = sorted(merged.values(), key=lambda r: -r["relevance_score"])[:_RETRIEVAL_TABLE_LIMIT]
        table_fqns = {r["qualified_name"] for r in capped}
        table_fqns |= _expand_relationships(source_id, table_fqns, session=session)
        return table_fqns, capped, remembered_terminology
    except (MemoryError, sqlite3.Error, OSError) as exc:
        # Phase 3.2 / Task 6 — an infrastructure failure (out of memory, a
        # broken/locked connection, a disk-level error) must never be
        # misreported as "found nothing". A prior implementation attempt
        # let exactly this happen (see data/search_service.py's
        # column-cache revert history: a MemoryError there was silently
        # swallowed by this same except-Exception block and surfaced to
        # the user as zero candidate tables). Recorded on the session (if
        # any) and re-raised as MetadataSearchFailedError so callers get a
        # distinct, named failure mode instead of an empty result
        # indistinguishable from a real no-match outcome. Deliberately
        # narrower than "except Exception" below — an application-level
        # exception (bad data shape, etc.) still degrades gracefully to
        # "no candidates", matching this function's documented contract;
        # only the infrastructure-failure category is promoted to a hard,
        # visible error.
        logger.error(
            "get_candidate_tables: infrastructure failure during retrieval for source_id=%s: %s",
            source_id, exc, exc_info=True,
        )
        if session is not None:
            session.mark_search_failed(f"{type(exc).__name__} during retrieval for source_id={source_id}")
        raise MetadataSearchFailedError(
            f"Metadata search infrastructure failed ({type(exc).__name__}) for source_id={source_id}"
        ) from exc
    except Exception:
        logger.warning(
            "get_candidate_tables: retrieval failed for source_id=%s, falling back",
            source_id, exc_info=True,
        )
        return set(), [], []
