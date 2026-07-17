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
Any empty or failed result returns an empty set so the caller falls back to
_collect_candidate_tables() unchanged.
"""
from __future__ import annotations

import logging

from data.db import get_connection
from data.search_service import search_metadata
from core.domains.rules import _DOMAIN_KEYWORDS, _CONFIDENCE_DENOMINATOR, _tokenize, _hits

logger = logging.getLogger(__name__)

# Domain filter is applied only when the top domain clears both gates —
# narrowing on a weak/ambiguous signal risks silently excluding relevant
# tables, so an unclear question searches unfiltered instead.
_DOMAIN_CONFIDENCE_MIN = 0.70
_DOMAIN_MARGIN_MIN = 0.15

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


def _search_tables(query_text: str, source_id: int, domain: str | None) -> list[dict]:
    result = search_metadata(
        q=query_text, source_id=source_id, asset_type="table",
        domain=domain, limit=_RETRIEVAL_TABLE_LIMIT,
    )
    return result.get("results") or []


def _latest_snapshot_id(conn, source_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM schema_snapshots WHERE source_id = ? "
        "ORDER BY snapshot_version DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return row["id"] if row else None


def _expand_relationships(source_id: int, table_fqns: set[str]) -> set[str]:
    """
    1-hop declared-FK expansion, capped, no-op when no active snapshot
    exists. Mirrors the relationship_status IN ('AUTO','APPROVED') read
    pattern knowledge_graph_service._build_fk_graph() already uses.
    """
    if not table_fqns:
        return set()
    conn = get_connection()
    try:
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


def get_candidate_tables(source_id: int, user_id: str, question: str, terms: list[str]) -> set[str]:
    """
    Sprint 1 AI Brain entry point — drop-in for _collect_candidate_tables().

    Sprint 1.3 adds two advisory, additive stages on top of the original
    combined-question search: per-term candidate diversity (Problem 1) and
    domain rescue (Problem 2). Neither replaces the base search or changes
    its scoring — both only add more candidates for the same final,
    score-sorted cap to choose from.

    Returns an empty set on any degraded, failed, or no-match condition so
    the caller (plan_business_query) falls back to _collect_candidate_tables()
    unchanged. Never raises.
    """
    if not terms:
        return set()
    try:
        query_text = " ".join(terms)
        domain = _select_domain_filter(question, terms)

        # Base: the original combined-question search, unchanged.
        ranked = _search_tables(query_text, source_id, domain)
        if not ranked and domain is not None:
            # A narrow domain filter must never be the sole reason for zero
            # results — retry once unfiltered before signalling fallback.
            ranked = _search_tables(query_text, source_id, None)

        merged: dict[str, dict] = {}
        _merge_best(merged, ranked)

        # Candidate diversity (Problem 1) — only meaningful with more than
        # one term; a single-term question is already fully covered by the
        # base search above, so skip the extra queries entirely.
        if len(terms) > 1:
            for term in terms:
                per_term = _search_tables(term, source_id, domain)[:_PER_TERM_LIMIT]
                _merge_best(merged, per_term)

        # Domain rescue (Problem 2) — only relevant when a confident domain
        # filter is actually narrowing the search; regardless of term count,
        # since the blind spot (a real table excluded by a domain-assignment
        # gap) can affect a single-term question just as much as a multi-term
        # one.
        if domain is not None:
            for term in terms:
                rescue = _search_tables(term, source_id, None)[:_RESCUE_LIMIT]
                if not rescue:
                    continue
                top_rescue_score = rescue[0]["relevance_score"]
                threshold = top_rescue_score * _RESCUE_RELEVANCE_RATIO
                high_relevance = [r for r in rescue if r["relevance_score"] >= threshold]
                _merge_best(merged, high_relevance)

        if not merged:
            return set()

        # Merge/dedupe done — apply the existing total candidate cap last,
        # preserving relevance-score ordering (and every penalty already
        # baked into those scores).
        capped = sorted(merged.values(), key=lambda r: -r["relevance_score"])[:_RETRIEVAL_TABLE_LIMIT]
        table_fqns = {r["qualified_name"] for r in capped}
        table_fqns |= _expand_relationships(source_id, table_fqns)
        return table_fqns
    except Exception:
        logger.warning(
            "get_candidate_tables: retrieval failed for source_id=%s, falling back",
            source_id, exc_info=True,
        )
        return set()
