"""
Business Query Planning Engine — Program 3 Phase 3.

Bridges a structured business request (question + concepts/measures/
dimensions/filters) to a safe query PLAN — candidate tables, candidate
columns, measures, dimensions, a join plan, and governance/trust warnings.

NO SQL generation. NO execution. NO LLM. Composes existing reads only:
  knowledge_graph_service.find_business_assets          — table/column discovery
  business_knowledge_service.get_table_business_context — column classification + governance
  semantic_layer_service.analyze_join_quality /
  semantic_layer_service.recommend_best_join_path       — join planning

Nothing here invents a table, column, or join that wasn't returned by one
of those three existing reads.
"""
import logging
import math
import re
import time

from data.db import get_connection
from data.knowledge_graph_service import find_business_assets, _compute_importance_score
from data.business_knowledge_service import get_table_business_context, get_table_business_contexts_batch
from data.request_metadata_session import MetadataSearchFailedError, RequestMetadataSession
from data.semantic_layer_service import analyze_join_quality, recommend_best_join_path
from core.dictionary.rule_classifier import _METRIC_TOKENS, _tokenize
from core.semantic.concept_resolver import (
    extract_query_intent, derive_analytics_intent, generate_compound_phrase_candidates,
    _COMPOUND_MAX_CANDIDATES_PER_QUESTION,
)
from core.semantic.compatibility_guard import infer_term_family, check_compatibility
from data.vocabulary_service import expand_concept, normalize_term
from data.semantic_retrieval_service import get_candidate_tables_with_ranking as _get_ai_candidate_tables_ranked
from data.concept_mapping_service import get_all_approved_mappings, get_synonym_canonical
from data.vocabulary_bootstrap_service import get_generated_vocabulary

logger = logging.getLogger(__name__)

# Step 4/5/8 — a candidate must clear this score (0-1) to be auto-selected.
# Named consistently with _LOW_CONF_THRESH (semantic_layer_service) /
# MIN_SUGGEST_CONFIDENCE (relationship_service).
_AUTO_SELECT_MIN_CONFIDENCE = 0.5

# Minimum score margin over the runner-up candidate to call a match unambiguous.
_AMBIGUITY_MARGIN = 0.15

_NUMERIC_DATA_TYPES = {"INTEGER", "DECIMAL"}
_DIMENSION_CARDINALITY_TIERS = {"LOW", "MEDIUM", "BINARY"}

_AGGREGATION_HINTS = {
    "count": "COUNT", "number": "COUNT", "qty": "COUNT", "quantity": "COUNT",
    "average": "AVG", "avg": "AVG", "mean": "AVG",
}

# ---------------------------------------------------------------------------
# Enterprise Authoritative Source Ranking (Milestone M-2) — negative naming
# signals for non-authoritative table variants. Token-exact matches (via the
# already-imported _tokenize) so naming-convention words never false-positive
# on legitimate tokens that merely contain them as a substring (e.g. "login"
# must not trigger "log", "important" must not trigger "import").
# ---------------------------------------------------------------------------
_NEGATIVE_NAME_TOKENS = {
    "temp", "tmp", "backup", "old", "archive", "history", "log", "msgs",
    "import", "staging", "snapshot", "copy", "generated",
}
# Compound brand/vendor words that tokenize apart (e.g. "ZoomInfoReady" ->
# zoom/info/ready) so a token-exact check can't catch them — matched as a
# substring on the raw lowercased name instead.
_NEGATIVE_NAME_SUBSTRINGS = ("zoominfo", "clickup")
# Dated-copy suffixes: a bare 19xx/20xx year, or a day+month+year stamp
# (e.g. "_17Feb2021").
_DATED_COPY_RE = re.compile(r"(19|20)\d{2}|\d{1,2}[A-Za-z]{3,9}\d{2,4}")

_NAMING_PENALTY_PER_HIT = 0.12
_NAMING_PENALTY_CAP = 0.35


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Term matching — deterministic token-overlap scoring, no NLP library, no LLM
# ---------------------------------------------------------------------------

def _tokens_near_match(a: str, b: str) -> bool:
    """
    True for identical tokens, or a simple singular/plural (or other short
    trailing-suffix) variant of one another — e.g. "client"/"clients",
    "invoice"/"invoices". Deliberately narrow (a short length tolerance on a
    shared prefix, never a general stemmer), so it matches genuine near-miss
    word forms without fuzzy-matching two unrelated words that merely share
    a short prefix (e.g. "can" must not near-match "candidate").
    """
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 3 and longer.startswith(shorter) and len(longer) - len(shorter) <= 2


def _near_match_jaccard(term_toks: set[str], toks: set[str]) -> float:
    """
    Token-overlap (Jaccard) similarity generalized to near-matching tokens
    (_tokens_near_match) instead of exact token equality — each term token
    is greedily paired with at most one still-unused candidate token, so a
    token is never counted as matching twice. Sorted iteration keeps pairing
    deterministic regardless of set ordering.
    """
    remaining = set(toks)
    matched_pairs = 0
    for t in sorted(term_toks):
        match = next((u for u in sorted(remaining) if _tokens_near_match(t, u)), None)
        if match is not None:
            remaining.discard(match)
            matched_pairs += 1
    union_size = len(term_toks) + len(toks) - matched_pairs
    return matched_pairs / union_size if union_size else 0.0


def _score_term_match_single(term: str, *texts: str | None) -> float:
    """
    0-1 similarity between a search term and one or more candidate text
    fields (column name, business label, meaning, table name). Near-match
    token-overlap (Jaccard) — deterministic and explainable.

    Milestone M-33 root-cause fix: this used to also grant a flat 0.75
    "substring" bonus whenever `term` appeared anywhere inside `text` as a
    raw character sequence, with no word-boundary awareness. That made a
    term buried inside one fragment of a longer, unrelated compound name
    (e.g. "client" inside "CB_CRM_CLIENT_CONTACTS") score almost as high as
    a table whose name basically IS the term (e.g. "clients" inside
    "ADF_Clients") — ~45/98 Enterprise Acceptance Suite questions failed
    with "Unresolved term(s)" because entity-count/table-candidate ranking
    tied several decoy tables at the same inflated score and correctly
    refused to guess among them. The Jaccard denominator (token union)
    naturally penalizes that dilution instead: the more unrelated tokens
    surround a matched token, the lower the score — while a genuine
    singular/plural near-miss (which the raw-substring check also existed
    to catch) still matches via _tokens_near_match.

    The reverse direction — a short candidate identity (e.g. a column's own
    business label, "Revenue") appearing entirely inside a longer query
    phrase — is safe evidence regardless of surrounding words, so it keeps
    its flat bonus unchanged; only the dilution-prone direction changed.
    """
    term_toks = set(_tokenize(term))
    if not term_toks:
        return 0.0

    best = 0.0
    term_lower = term.lower().strip()
    for text in texts:
        if not text:
            continue
        text_lower = text.lower()
        if term_lower == text_lower:
            return 1.0
        toks = set(_tokenize(text))
        if not toks:
            continue
        score = _near_match_jaccard(term_toks, toks)
        if text_lower in term_lower:
            score = max(score, 0.75)
        # A term that arrives as one glued, unspaced blob (e.g. a caller
        # pre-lowercases/concatenates "TotalStudents" into "totalstudents",
        # or expand_concept's own naive singularization turns it into
        # "totalstudent") can't near-match token-by-token against text that
        # tokenizes into multiple words ("total"+"students") — neither
        # token alone is close in length to the whole glued term. Compare
        # the raw strings as one more "token" pair: this only fires when
        # term and text are close in overall length (_tokens_near_match's
        # own tight tolerance), so it cannot resurrect the dilution bug
        # above (a short term is never close in length to a long, unrelated
        # compound name like "cb_crm_client_contacts").
        if _tokens_near_match(term_lower, text_lower):
            score = max(score, 0.9)
        best = max(best, score)
    return round(best, 4)


def _score_term_match(term: str, *texts: str | None) -> float:
    """
    Milestone M-5, Part 2/6: same scoring as `_score_term_match_single`, but
    tried against every governed synonym expansion of `term` (via
    `vocabulary_service.expand_concept` — reuses the existing
    `data.search_service._SynonymExpander`/`data/synonyms.json`
    unchanged), keeping the best score across all expansions. So "Customers"
    matches concept term "client" at full strength via the synonym group,
    not only through a lucky substring match.
    """
    best = 0.0
    for expanded in expand_concept(term) or [term]:
        best = max(best, _score_term_match_single(expanded, *texts))
    return best


# ---------------------------------------------------------------------------
# Enterprise Authoritative Source Ranking (Milestone M-2)
#
# Extends the name-match score above with the business-importance/governance
# signals ToolSmithAI already computes and stores per table, so ranking
# reflects real evidence (approval, domain/entity assignment, relationships,
# row count, naming convention) instead of name-string coincidence alone.
# Reuses knowledge_graph_service._compute_importance_score — the same
# formula explain_table() already shows — rather than a second, diverging
# importance calculation.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sprint 2, Signal #2 — Weighted All-Status Relationship Centrality
#
# Bounded, confidence- and status-weighted evidence from ALL relationship
# statuses (ctx["relationship_evidence"] — see get_table_business_context),
# so canonicality ranking can treat a PENDING edge as weak corroborating
# evidence — never as a joinable edge. Join planning's trusted contract
# (AUTO/APPROVED-only) is untouched: semantic_layer_service._load_edges and
# ctx["relationships"] both still refuse PENDING outright.
#
# AUTO/APPROVED edges are deliberately EXCLUDED from this function's total —
# they already earn full credit via _score_table_authority's own rel_count
# bonus above, so counting them again here would double-count the same
# trusted evidence.
#
# Weight model (deliberately conservative — corroborating evidence, not a
# trust decision): PENDING -> 0.4, anything else (REJECTED, unrecognised) ->
# 0.0. Multiplied by the edge's own confidence (0-1) and a direction weight
# (inbound 1.25x, outbound 1.0x — being referenced by other tables is a
# stronger centrality signal than merely referencing one, the same intuition
# knowledge_graph_service._compute_importance_score already applies to raw
# referenced_by_count). Bounded to a modest +0.05 max — well under
# _AMBIGUITY_MARGIN (0.15), so this signal alone can never manufacture an
# auto-selected winner; it can only help separate an already-near tie.
# ---------------------------------------------------------------------------
_REL_STATUS_WEIGHT = {"PENDING": 0.4}
_REL_DIRECTION_WEIGHT = {"inbound": 1.25, "outbound": 1.0}
_REL_CENTRALITY_SCALE = 0.01
_REL_CENTRALITY_MAX_BONUS = 0.05


def _score_relationship_centrality(relationship_evidence: list[dict] | None) -> dict:
    """
    Pure scoring helper over get_table_business_context()'s
    `relationship_evidence` (ALL relationship_status values). Returns
    {"bonus": float, "reasons": list[str], "diagnostics": dict}.
    """
    weighted_total = 0.0
    counted = 0
    for edge in relationship_evidence or []:
        status = edge.get("relationship_status")
        if status in ("AUTO", "APPROVED"):
            continue  # already counted by the trusted rel_count bonus
        status_weight = _REL_STATUS_WEIGHT.get(status, 0.0)
        if status_weight <= 0.0:
            continue
        confidence = edge.get("confidence") or 0.0
        if confidence <= 0.0:
            continue
        direction_weight = _REL_DIRECTION_WEIGHT.get(edge.get("direction"), 1.0)
        weighted_total += status_weight * confidence * direction_weight
        counted += 1

    raw_bonus = _REL_CENTRALITY_SCALE * weighted_total
    bonus = round(min(_REL_CENTRALITY_MAX_BONUS, raw_bonus), 4)

    reasons = []
    if bonus > 0:
        reasons.append(
            f"Relationship centrality evidence ({counted} lower-confidence "
            f"linked table(s), +{bonus:.4f})"
        )

    return {
        "bonus": bonus,
        "reasons": reasons,
        "diagnostics": {
            "edges_counted": counted,
            "weighted_total": round(weighted_total, 4),
            "raw_bonus_before_cap": round(raw_bonus, 4),
        },
    }


# Accuracy Safety A3.1 — Name-Relevance Gate. A table with little or no
# textual relevance to the requested term must not win primarily on generic
# "this table is important somewhere in the schema" evidence (confirmed
# real cases: dnnuser.Users/aspnet_Users — ASP.NET/DNN framework identity
# tables with genuinely high row-count/master/relationship-centrality
# evidence — outranking the actual candidate/interview business tables).
# Below this name_score floor, positive authority evidence is linearly
# suppressed toward zero as relevance approaches zero; at/above it, behavior
# is unchanged. This is a floor, not a cliff — a table need only clear a
# small amount of real textual relevance to keep its full authority bonus.
_NAME_RELEVANCE_GATE_FLOOR = 0.25


def _score_table_authority(table_fqn: str, ctx: dict, name_score: float | None = None) -> dict:
    """
    0-1-ish bonus/penalty (clamped to [-0.5, 0.5]) plus human-readable reasons,
    derived entirely from fields get_table_business_context() already returns
    for `table_fqn` — no new reads, no invented metadata.

    `name_score` (Accuracy Safety A3.1) is the caller's already-computed
    term-vs-table-identity match score (0-1) — the same value every existing
    caller already calculates immediately before this function via
    _score_term_match(term, table_name, business_name, description). When
    omitted (None), behavior is byte-identical to before this change (full
    authority weight, no gating) — preserves every existing caller
    (data/dictionary_curation_service.py, and every test that calls this
    function positionally without a third argument) exactly as-is.

    Positive, non-naming authority evidence (importance/domain/entity/
    relationship/row-count bonuses, plus the new root/master/junction
    structural terms below) is scaled by a name-relevance factor: at or
    above _NAME_RELEVANCE_GATE_FLOOR it is unchanged (scale 1.0); below it,
    scaled down linearly toward 0 as name_score approaches 0. Existing
    negative evidence (naming penalty, view penalty, empty-table penalty)
    is never scaled — a table's archive/backup/empty-table evidence applies
    in full regardless of how relevant its name is to the query term.
    """
    reasons: list[str] = []
    bonus = 0.0          # fixed terms — never scaled (existing penalties, preserved as-is)
    positive_bonus = 0.0  # name-relevance-gated terms — scaled by authority_scale

    if name_score is None:
        authority_scale = 1.0
    elif name_score >= _NAME_RELEVANCE_GATE_FLOOR:
        authority_scale = 1.0
    else:
        authority_scale = max(0.0, name_score) / _NAME_RELEVANCE_GATE_FLOOR

    dict_row = ctx.get("dictionary")
    domain_row = ctx.get("domain")
    entity_row = ctx.get("entity")
    profiling = ctx.get("profiling")
    relationships = ctx.get("relationships") or {}
    governance = ctx.get("governance") or {}
    table_type = (ctx.get("table") or {}).get("table_type")

    # Reused importance formula: dictionary approval, referenced-by-count,
    # root-table flag, table class, PII presence.
    importance = _compute_importance_score(profiling, dict_row)
    if importance:
        positive_bonus += importance * 0.30
    if governance.get("dictionary_approved"):
        reasons.append("Dictionary Approved")
    if profiling and profiling.get("is_root_table"):
        reasons.append("Root/primary table")
    if profiling and profiling.get("table_class") in ("Master", "Reference"):
        reasons.append(f"{profiling['table_class']} table")

    # Sprint 2, Signal #1 — Confidence-Aware Semantic Scoring: scale each
    # bonus by the assignment's own confidence (0-1, already computed by
    # domain_service/entity_service) instead of granting the full bonus for
    # any non-"Unknown" assignment regardless of how confident it is. Ceiling
    # is unchanged (0.05 / 0.07 at confidence 1.0), so the overall bonus
    # range and every other additive signal below are unaffected.
    if governance.get("domain_assigned"):
        positive_bonus += 0.05 * (domain_row["confidence"] or 0.0)
        reasons.append(f"Domain = {domain_row['domain']} (confidence {domain_row['confidence']:.0%})")
    if governance.get("entity_assigned"):
        positive_bonus += 0.07 * (entity_row["confidence"] or 0.0)
        reasons.append(f"Entity = {entity_row['entity']} (confidence {entity_row['confidence']:.0%})")

    rel_count = len(relationships.get("outbound") or []) + len(relationships.get("inbound") or [])
    if rel_count:
        positive_bonus += min(0.12, 0.02 * rel_count)
        reasons.append(f"Relationship coverage ({rel_count} linked table(s))")

    centrality = _score_relationship_centrality(ctx.get("relationship_evidence"))
    positive_bonus += centrality["bonus"]
    reasons.extend(centrality["reasons"])

    if profiling:
        row_count = profiling.get("exact_row_count")
        if row_count is None:
            row_count = profiling.get("estimated_row_count")
        if row_count:
            positive_bonus += min(0.15, 0.025 * math.log10(row_count + 1))
            reasons.append(f"Row count evidence ({row_count:,} rows)")
        elif row_count == 0:
            bonus -= 0.10
            reasons.append("Table is empty (0 rows)")

    # Accuracy Safety A3.1 — bounded structural evidence, un-diluted from the
    # ×0.30-discounted importance term above (root/Master already contribute
    # there too; this is a modest, capped top-up, not a doubling — overall
    # bonus stays clamped to [-0.5, 0.5] as before). is_junction_table is a
    # new signal: computed and stored during profiling but never previously
    # read by any ranking function — a junction/bridge table is almost never
    # the authoritative root business entity. All three terms are gated by
    # authority_scale like every other positive/structural signal above —
    # structural evidence about an otherwise textually-irrelevant table
    # must not bypass the name-relevance gate either.
    if profiling and profiling.get("is_root_table"):
        positive_bonus += 0.06
    if profiling and profiling.get("table_class") == "Master":
        positive_bonus += 0.05
    if profiling and profiling.get("is_junction_table"):
        positive_bonus -= 0.15
        reasons.append("Junction/bridge table (unlikely to be the root business entity)")

    if table_type == "VIEW":
        bonus -= 0.05
        reasons.append("View, not a base table")

    table_name = table_fqn.split(".")[-1]
    name_lower = table_name.lower()
    tokens = set(_tokenize(table_name))
    hits = sorted(tokens & _NEGATIVE_NAME_TOKENS)
    hits += [kw for kw in _NEGATIVE_NAME_SUBSTRINGS if kw in name_lower]
    if _DATED_COPY_RE.search(table_name):
        hits.append("dated copy")
    if hits:
        penalty = min(_NAMING_PENALTY_CAP, _NAMING_PENALTY_PER_HIT * len(hits))
        bonus -= penalty
        reasons.append(f"Naming penalty: {', '.join(hits)}")
    else:
        reasons.append("No temporary/backup/archive naming indicators")

    bonus += positive_bonus * authority_scale
    return {"bonus": round(max(-0.5, min(0.5, bonus)), 4), "reasons": reasons}


def _concept_mapping_lookup(
    concept_mappings_by_term: dict[str, list[dict]] | None, term: str,
) -> dict[tuple[str, str], dict]:
    """
    Phase 2, Step 7 — builds a (table_fqn, column_name) lookup for one term
    from an already-fetched, whole-source approved-mappings dict
    (data.concept_mapping_service.get_all_approved_mappings, called ONCE per
    plan_business_query() call — not once per term/resolver call/candidate
    column, which regressed this module's own test suite ~6x when tried).
    '' means table-level (matches concept_table_mappings' own '' sentinel —
    see data/models.py). concept_mappings_by_term only ever contains
    AUTO_APPROVED/HUMAN_APPROVED rows (get_all_approved_mappings' own
    contract) — GENERATED/SUGGESTED mappings are invisible here, same as an
    unapproved dictionary entry never earning _score_table_authority's
    approval bonus.
    """
    if not concept_mappings_by_term:
        return {}
    normalized = normalize_term(term)
    return {
        (m["table_fqn"], m.get("column_name") or ""): m
        for m in concept_mappings_by_term.get(normalized, [])
    }


# Capped well above typical _score_table_authority bonuses (clamped to
# [-0.5, 0.5]) so an approved concept mapping is decisive in the common
# case, but candidates still flow through the same _AUTO_SELECT_MIN_
# CONFIDENCE/_AMBIGUITY_MARGIN gate as everything else — this is a bias
# (one more additive scoring signal), never a bypass.
_CONCEPT_MAPPING_BONUS = 0.4


def _concept_mapping_bonus(
    mapping_lookup: dict[tuple[str, str], dict], term: str, table_fqn: str, column_name: str | None,
) -> dict:
    """Returns {"bonus": float, "reason": str | None} for one candidate,
    against a lookup already computed once per term by
    _concept_mapping_lookup (never a fresh DB read per candidate)."""
    mapping = mapping_lookup.get((table_fqn, column_name or ""))
    if mapping is None:
        return {"bonus": 0.0, "reason": None}
    return {
        "bonus": _CONCEPT_MAPPING_BONUS,
        "reason": (
            f"Approved concept mapping: '{term}' -> this "
            f"{'column' if column_name else 'table'} "
            f"(confidence {mapping.get('confidence') or 0.0:.0%})."
        ),
    }


# Enterprise Phase 4 — Autonomous Semantic Bootstrapping and Business
# Vocabulary. Both bonuses sit below _CONCEPT_MAPPING_BONUS (0.4) so an
# approved human mapping always outranks auto-derived vocabulary — enforced
# structurally via _generated_vocabulary_bonus's own mutual-exclusion guard,
# not merely by these constants' relative size. _GENERATED_VOCAB_BONUS_MEDIUM
# is deliberately smaller than _AUTO_SELECT_MIN_CONFIDENCE (0.5): a lone
# MEDIUM-tier generated term must never, by itself, auto-select a candidate
# past the ambiguity gate — a bias, never a bypass, same discipline
# _CONCEPT_MAPPING_BONUS's own docstring already establishes.
_GENERATED_VOCAB_BONUS_HIGH = 0.25
_GENERATED_VOCAB_BONUS_MEDIUM = 0.12


def _generated_vocab_lookup(
    generated_vocab_by_term: dict[str, list[dict]] | None, term: str,
) -> dict[tuple[str, str], dict]:
    """Builds a (table_fqn, column_name) lookup for one term from an
    already-fetched, whole-source generated-vocabulary dict
    (data.vocabulary_bootstrap_service.get_generated_vocabulary, called ONCE
    per plan_business_query() call — mirrors _concept_mapping_lookup's own
    per-call-not-per-candidate discipline). '' means table-level, matching
    concept_table_mappings'/generated_business_vocabulary's own '' sentinel.
    LOW-tier rows are excluded here — never surfaced to scoring, same
    contract data.semantic_retrieval_service's retrieval-stage merge already
    enforces."""
    if not generated_vocab_by_term:
        return {}
    normalized = normalize_term(term)
    return {
        (row["table_fqn"], row.get("column_name") or ""): row
        for row in generated_vocab_by_term.get(normalized, [])
        if row.get("confidence_tier") != "LOW"
    }


def _generated_vocabulary_bonus(
    vocab_lookup: dict[tuple[str, str], dict], term: str, table_fqn: str, column_name: str | None,
    *, concept_bonus_active: bool,
) -> dict:
    """Returns {"bonus": float, "reason": str | None, "generated_term": str | None}
    for one candidate. Mutually exclusive with _concept_mapping_bonus for the
    exact same (table_fqn, column_name): when an approved concept mapping
    already applied there, this always returns a zero bonus — an approved
    human mapping must structurally always win, never stack with or be
    diluted by a lower-confidence auto-derived signal for the same slot."""
    if concept_bonus_active:
        return {"bonus": 0.0, "reason": None, "generated_term": None}
    row = vocab_lookup.get((table_fqn, column_name or ""))
    if row is None:
        return {"bonus": 0.0, "reason": None, "generated_term": None}
    tier = row.get("confidence_tier")
    bonus = _GENERATED_VOCAB_BONUS_HIGH if tier == "HIGH" else _GENERATED_VOCAB_BONUS_MEDIUM
    return {
        "bonus": bonus,
        "reason": (
            f"Generated vocabulary: '{term}' -> this {'column' if column_name else 'table'} "
            f"(auto-derived, {(tier or 'medium').lower()} confidence)."
        ),
        "generated_term": row.get("term"),
    }


# ---------------------------------------------------------------------------
# Phase 3, Step 4 — remembered-terminology explainability. Pure evidence,
# never consulted for scoring/selection — mirrors
# semantic_retrieval_service._remembered_terminology_evidence exactly (same
# shape, same exclusions) since the same substitution runs in both files.
# ---------------------------------------------------------------------------

def _remembered_terminology_evidence(source_id: int, terms: list[str], *, session=None) -> list[dict]:
    """
    One evidence record per term for which a remembered synonym
    (concept_mapping_service.get_synonym_canonical) actually changed the
    term. No record when: the term is unknown to concept_term_synonyms; the
    canonical term is used directly; the mapping would be self-referential;
    or the mapping belongs to a different source — get_synonym_canonical
    already excludes all of these (it returns None unless a different,
    source-scoped canonical term was explicitly taught). Deduplicated by
    (original_term, canonical_term).
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


def _merge_remembered_terminology(*evidence_lists: list[dict]) -> list[dict]:
    """Combines evidence collected independently in this file's own
    substitution step and semantic_retrieval_service's, deduplicated by
    (original_term, canonical_term) so a term substituted in both places
    (or repeated across concepts/measures/dimensions) yields exactly one
    record in the final query_plan."""
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for evidence_list in evidence_lists:
        for item in evidence_list:
            pair = (item["original_term"], item["canonical_term"])
            if pair in seen:
                continue
            seen.add(pair)
            merged.append(item)
    return merged


def _rank_key(candidate: dict) -> float:
    """
    Sorting/threshold/margin key — the UNCLAMPED name_score + authority_bonus
    sum. "score" (name_score + bonus, clamped to [0, 1]) is kept as the
    public confidence-like field (it feeds _compute_confidence's 0-100
    output), but clamping it for ranking purposes would collapse distinct
    high-evidence candidates to an identical 1.0 ceiling and silently erase
    the very differentiation Milestone M-2 exists to produce — confirmed
    against real CCPP metadata, where several genuinely different-quality
    "clients"/"projects" candidates all clamped to 1.0000 before this fix.
    """
    return candidate["name_score"] + candidate["authority_bonus"]


# ---------------------------------------------------------------------------
# Step 3 — Table/column discovery (composes find_business_assets)
# ---------------------------------------------------------------------------

def _collect_candidate_tables(source_id: int, user_id: str, terms: list[str]) -> set[str] | None:
    """
    Run find_business_assets once per term (and, per Milestone M-5 Part 2,
    once per governed synonym expansion of that term — reuses
    vocabulary_service.expand_concept, the same expansion enterprise search
    already applies), union the candidate table_fqns. Returns None on an
    ownership failure (propagated to the caller's own upfront check — this
    should not happen in practice since plan_business_query already
    verifies ownership first).

    Unbounded (no LIMIT, no domain pre-filter) — no longer called from
    plan_business_query as of the Semantic Retrieval Integration change,
    which relies on get_candidate_tables()'s bounded result (including an
    empty one) rather than falling back to this scan. Kept in place only
    because other callers/tests may still exercise it directly; do not
    reintroduce it as an automatic fallback in the business-query path.
    """
    candidate_tables: set[str] = set()
    expanded_terms = list(dict.fromkeys(
        t for term in terms for t in (expand_concept(term) or [term])
    ))
    for term in expanded_terms:
        assets = find_business_assets(source_id, user_id, term=term)
        if assets is None:
            return None
        for t in assets.get("tables", []):
            candidate_tables.add(t["table_fqn"])
        for c in assets.get("columns", []):
            candidate_tables.add(c["table_fqn"])
    return candidate_tables


# ---------------------------------------------------------------------------
# Step 4/5 — Measure / dimension classification
#
# Primary signal: data_dictionary_columns.is_metric / is_dimension / is_date
# (already computed by core/dictionary/rule_classifier.classify_column).
# Fallback (no dictionary entry yet): numeric type + _METRIC_TOKENS overlap
# for measures; cardinality tier + PII-confirmation for dimensions. The
# fallback reuses the SAME token set dictionary generation uses, rather than
# inventing a second, possibly-diverging heuristic.
# ---------------------------------------------------------------------------

def _is_metric_column(col: dict) -> bool:
    dic = col.get("dictionary")
    if dic is not None:
        return bool(dic.get("is_metric"))
    schema = col.get("schema")
    if not schema or schema.get("data_type") not in _NUMERIC_DATA_TYPES:
        return False
    return bool(set(_tokenize(col["column_name"])) & _METRIC_TOKENS)


def _is_dimension_column(col: dict) -> bool:
    dic = col.get("dictionary")
    if dic is not None:
        # Dictionary classification is definitive — trust it fully.
        # Fall through to profiling only when NO dictionary entry exists.
        return bool(dic.get("is_dimension") or dic.get("is_date"))
    profiling = col.get("profiling")
    if not profiling:
        return False
    if profiling.get("semantic_type") == "date":
        return True
    if profiling.get("pii_name_heuristic") and not profiling.get("pii_confirmed"):
        return False
    return profiling.get("cardinality_tier") in _DIMENSION_CARDINALITY_TIERS


def _related_table_names(ctx: dict | None, self_table_fqn: str) -> list[str]:
    """Accuracy Program A4, Fix #3 — distinct related table_fqns from this
    table's own already-fetched relationship rows (get_table_business_context
    reads these once per table regardless; no new query). Display-only,
    consumed solely to build a factual "connected to X" clarification reason.
    List-based (not a set) so output order is stable run-to-run."""
    relationships = (ctx or {}).get("relationships") or {}
    related: list[str] = []
    for rel in list(relationships.get("outbound") or []) + list(relationships.get("inbound") or []):
        related_fqn = rel.get("to_table_fqn") or rel.get("from_table_fqn")
        if related_fqn and related_fqn != self_table_fqn and related_fqn not in related:
            related.append(related_fqn)
    return related[:5]


def _score_candidates(
    term: str, table_fqn: str, columns: list[dict], predicate,
    mapping_lookup: dict[tuple[str, str], dict] | None = None,
    vocab_lookup: dict[tuple[str, str], dict] | None = None,
    ctx: dict | None = None,
) -> list[dict]:
    mapping_lookup = mapping_lookup or {}
    vocab_lookup = vocab_lookup or {}
    # Accuracy Program A4, Fix #2/#3 — display-only label/evidence metadata,
    # read from the same table context every caller already has in hand (no
    # new fetch). Never read by scoring/ranking (_rank_key, _AMBIGUITY_MARGIN,
    # etc.) — consumed only by core/answering/explanation_builder.py for the
    # clarification option's label/object_type/domain/entity/why_relevant.
    table_business_name = ((ctx or {}).get("dictionary") or {}).get("business_name")
    entity_name = ((ctx or {}).get("entity") or {}).get("entity")
    table_object_type = ((ctx or {}).get("table") or {}).get("table_type")
    domain_name = ((ctx or {}).get("domain") or {}).get("domain")
    relationship_related_tables = _related_table_names(ctx, table_fqn)
    out = []
    for col in columns:
        if not predicate(col):
            continue
        dic = col.get("dictionary") or {}
        prof = col.get("profiling") or {}
        name_score = _score_term_match(term, col["column_name"], dic.get("business_label"), dic.get("meaning"))
        # Accuracy Safety A3.1 parity fix (Phase 4, Milestone 1) — the
        # Name-Relevance Gate (_score_table_authority's name_score param)
        # was already applied at the table level by _resolve_concept/
        # _resolve_count_all, but never here: authority used to be computed
        # ONCE per table (by the caller, before this per-column loop) with
        # no name_score at all, so a column with zero textual relevance to
        # `term` could still inherit its table's full generic-importance
        # bonus. Computed per CANDIDATE COLUMN, using that column's own
        # name_score, since authority must be gated by the same evidence
        # that's about to be added to it.
        table_authority = _score_table_authority(table_fqn, ctx or {}, name_score)
        concept_mapping = _concept_mapping_bonus(mapping_lookup, term, table_fqn, col["column_name"])
        generated_vocab = _generated_vocabulary_bonus(
            vocab_lookup, term, table_fqn, col["column_name"],
            concept_bonus_active=bool(concept_mapping["bonus"]),
        )
        # Phase 2, Step 7 — folded into the SAME authority_bonus field
        # _rank_key reads (unclamped name_score + authority_bonus), not just
        # the clamped display "score" below: clamping score to [0, 1] would
        # otherwise cap an approved-mapped candidate's usable margin at
        # (1.0 - competitor_score), which collapses to well under
        # _AMBIGUITY_MARGIN whenever a competitor already scores highly
        # (reproduced: two ~0.88-scoring candidates left only a 0.12 margin
        # after a +0.4 bonus, never clearing the 0.15 gate) — the same
        # clamping-collapse problem _rank_key's own docstring already
        # documents fixing for the table-authority bonus alone.
        total_bonus = table_authority["bonus"] + concept_mapping["bonus"] + generated_vocab["bonus"]
        score = max(0.0, min(1.0, name_score + total_bonus))
        ranking_reasons = list(table_authority["reasons"])
        if concept_mapping["reason"]:
            ranking_reasons.append(concept_mapping["reason"])
        if generated_vocab["reason"]:
            ranking_reasons.append(generated_vocab["reason"])
        out.append({
            "table_fqn":        table_fqn,
            "column_name":      col["column_name"],
            "business_label":   dic.get("business_label"),
            "table_business_name": table_business_name,
            "entity_name":      entity_name,
            "table_object_type": table_object_type,
            "domain_name":      domain_name,
            "relationship_related_tables": relationship_related_tables,
            "score":            score,
            "name_score":       name_score,
            "authority_bonus":  total_bonus,
            "ranking_reasons":  ranking_reasons,
            "is_approved":      bool(dic.get("is_approved")) if dic else False,
            "data_type":        (col.get("schema") or {}).get("data_type"),
            # Milestone Phase 6.1 — Semantic Correctness Guard: the column's
            # own already-computed profiling semantic type, carried through
            # so _resolve_term can check it against the requested term's
            # inferred concept family before ever auto-selecting this
            # candidate. No new read — already present on every column dict
            # returned by get_table_business_context.
            "semantic_type":    prof.get("semantic_type"),
            # Enterprise Phase 4 — set only when a generated-vocabulary bonus
            # actually applied, so plan_business_query can surface "Resolved
            # using database vocabulary" evidence for the winning candidate
            # only (never for every scored candidate).
            "generated_vocabulary_term": generated_vocab["generated_term"],
        })
    return out


def _resolve_term(
    term: str, table_contexts: dict[str, dict], kind: str,
    concept_mappings: dict[str, list[dict]] | None = None,
    generated_vocab: dict[str, list[dict]] | None = None,
    grounding: dict | None = None,
) -> dict:
    """
    Step 4/5/8. Score every eligible column across all candidate tables
    against `term`, rank, and only auto-select when the top candidate clears
    _AUTO_SELECT_MIN_CONFIDENCE with a clear margin over the runner-up.
    Never silently chooses a low-confidence option.

    Each column's score is the name-match score plus its own table's
    authority bonus (_score_table_authority — Milestone M-2), so a column in
    a well-governed, well-populated production table outranks the identically
    named column in a `_temp`/unapproved/low-evidence one. Phase 2, Step 7
    adds an approved concept_table_mappings bonus on top (see
    _concept_mapping_bonus) when concept_mappings is provided.

    Day 2C, Task 4 — when `term` itself names a grounded target entity (e.g.
    a "recruiter" dimension while Recruiter is grounded), column search is
    narrowed to that entity's own canonical table first (_narrow_to_grounded_
    table) rather than scanning the whole candidate pool.

    Day 2E, Task 1 — a status/attribute word for a grounded entity is
    resolved directly against that entity's OWN contract-approved table
    (_resolve_contract_status_column) — never scored via the ordinary
    metric/dimension column predicate across a multi-table pool. extract_
    terms() classifies a bare attribute word like "status" as a MEASURE
    term as often as a dimension term (no "by"-clause dual-classification),
    so this check applies regardless of `kind`. A term naming the grounded
    entity itself is still restricted to just that one table before the
    ordinary scoring loop runs (never dropped to the full pool).
    """
    contract_hit = _resolve_entity_role_from_contract(term, grounding)
    if contract_hit and contract_hit["table_fqn"] in table_contexts:
        if contract_hit["role"] == "status":
            return _resolve_contract_status_column(term, kind, contract_hit["table_fqn"], table_contexts[contract_hit["table_fqn"]])
        table_contexts = {contract_hit["table_fqn"]: table_contexts[contract_hit["table_fqn"]]}
    else:
        table_contexts = _narrow_to_grounded_table(term, table_contexts, grounding)
    predicate = _is_metric_column if kind == "measure" else _is_dimension_column
    mapping_lookup = _concept_mapping_lookup(concept_mappings, term)
    vocab_lookup = _generated_vocab_lookup(generated_vocab, term)
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        candidates.extend(
            _score_candidates(
                term, table_fqn, ctx["columns"], predicate, mapping_lookup, vocab_lookup, ctx=ctx,
            )
        )

    candidates.sort(key=lambda c: -_rank_key(c))

    selected = None
    warnings: list[dict] = []
    semantic_compatibility: dict | None = None
    if not candidates:
        warnings.append({
            "type": f"missing_{kind}", "severity": "MEDIUM",
            "message": f"No {kind} candidate found for '{term}'.",
        })
    else:
        top = candidates[0]
        top_key = _rank_key(top)
        runner_up_key = _rank_key(candidates[1]) if len(candidates) > 1 else 0.0
        if top_key >= _AUTO_SELECT_MIN_CONFIDENCE and (top_key - runner_up_key) >= _AMBIGUITY_MARGIN:
            # Milestone Phase 6.1 — Semantic Correctness Guard. The score+
            # margin gate above only proves the match is unambiguous among
            # the candidates found; it says nothing about whether the term's
            # OWN concept family agrees with the winning column's own
            # profiling.semantic_type (e.g. a calendar word like "year"
            # uncontested-matching an AMOUNT-typed column by name substring
            # alone). Checked only for the winning candidate, and only ever
            # adds a refusal on top of the existing gate — never loosens it.
            term_family = infer_term_family(term)
            compat = check_compatibility(term, term_family, top.get("semantic_type"))
            if compat.compatible:
                selected = top
            else:
                suggested = next(
                    (
                        c for c in candidates[1:5]
                        if check_compatibility(term, term_family, c.get("semantic_type")).compatible
                        and _rank_key(c) >= _AUTO_SELECT_MIN_CONFIDENCE
                    ),
                    None,
                )
                semantic_compatibility = {
                    "compatible":         False,
                    "requested_measure":  term,
                    "resolved_concept":   f"{top['table_fqn']}.{top.get('column_name')}",
                    "term_family":        compat.term_family,
                    "column_family":      compat.column_family,
                    "reason":             compat.reason,
                    "confidence":         round(top["score"], 4),
                    "suggested": (
                        {"table_fqn": suggested["table_fqn"], "column_name": suggested.get("column_name")}
                        if suggested else None
                    ),
                }
                warnings.append({
                    "type": f"semantic_incompatible_{kind}", "severity": "HIGH",
                    "message": compat.reason,
                })
        else:
            warnings.append({
                "type": f"ambiguous_{kind}", "severity": "MEDIUM",
                "message": (
                    f"Multiple {kind} candidates for '{term}' with similar confidence; "
                    "no single column was auto-selected."
                    if len(candidates) > 1 else
                    f"No confident {kind} match for '{term}' "
                    f"(best score {top['score']:.2f} below the {_AUTO_SELECT_MIN_CONFIDENCE:.2f} threshold)."
                ),
            })

    return {
        "term":       term,
        "selected":   selected,
        "candidates": candidates[:5],
        "warnings":   warnings,
        "semantic_compatibility": semantic_compatibility,
    }


def _resolve_count_all(
    term: str, table_contexts: dict[str, dict],
    concept_mappings: dict[str, list[dict]] | None = None,
    generated_vocab: dict[str, list[dict]] | None = None,
) -> dict | None:
    """
    Bare row-count support ("How many clients?", "Number of students").

    A measure term that resolves to no metric COLUMN may still name a real
    TABLE directly (the term IS the entity, not an attribute of it). Scores
    `term` against each candidate table's own short name, PLUS its approved
    dictionary business_name/description when present — the same text
    fields _resolve_concept() already scores against (parity fix: this
    function used to compare the raw table name alone, so an approved
    business_name like "Clients" on a poorly-named table never helped it
    outrank a same-token decoy) — via the same _score_term_match/
    _AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN rules already used for
    column matching. No new metadata read: business_name/description are
    already present on ctx["dictionary"] (get_table_business_context's
    existing return shape) — never invents a table that wasn't already a
    candidate in table_contexts.

    Score is the table-name match plus the table's own authority bonus
    (_score_table_authority — Milestone M-2: dictionary approval, domain/
    entity assignment, relationship coverage, row count, naming-convention
    penalties), so among several same-name-scoring "clients" tables the one
    with real evidence of being the authoritative source ranks first. Phase
    2, Step 7 adds an approved concept_table_mappings bonus on top (see
    _concept_mapping_bonus) when concept_mappings is provided.

    Returns {"selected": dict | None, "candidates": list[dict]} — "selected"
    is shaped like _resolve_term's own (table_fqn/column_name/business_label/
    score/is_approved/data_type) but with column_name=None, meaning
    "COUNT(*) on this table" rather than a specific column —
    sql_planning_service/sql_generation_service render this as COUNT(*),
    never SELECT *. "candidates" is always populated (top 5, ranked) so the
    ranking stays explainable even when nothing clears the auto-select
    threshold/margin. Returns None only when there are no candidate tables
    at all.
    """
    mapping_lookup = _concept_mapping_lookup(concept_mappings, term)
    vocab_lookup = _generated_vocab_lookup(generated_vocab, term)
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        table_name = table_fqn.split(".")[-1]
        dictionary = ctx.get("dictionary") or {}
        # Accuracy Program A4, Fix #2/#3 — same already-fetched ctx, read for
        # display-only label/evidence metadata (see _score_candidates' matching
        # note).
        entity_row = ctx.get("entity") or {}
        domain_row = ctx.get("domain") or {}
        table_object_type = (ctx.get("table") or {}).get("table_type")
        relationship_related_tables = _related_table_names(ctx, table_fqn)
        name_score = _score_term_match(term, table_name, dictionary.get("business_name"), dictionary.get("description"))
        authority = _score_table_authority(table_fqn, ctx, name_score)
        concept_mapping = _concept_mapping_bonus(mapping_lookup, term, table_fqn, None)
        generated_vocab_bonus = _generated_vocabulary_bonus(
            vocab_lookup, term, table_fqn, None, concept_bonus_active=bool(concept_mapping["bonus"]),
        )
        # See _score_candidates' matching comment: folded into authority_bonus
        # (which _rank_key sums unclamped), not just the clamped score below.
        total_bonus = authority["bonus"] + concept_mapping["bonus"] + generated_vocab_bonus["bonus"]
        score = max(0.0, min(1.0, name_score + total_bonus))
        ranking_reasons = list(authority["reasons"])
        if concept_mapping["reason"]:
            ranking_reasons.append(concept_mapping["reason"])
        if generated_vocab_bonus["reason"]:
            ranking_reasons.append(generated_vocab_bonus["reason"])
        candidates.append({
            "table_fqn": table_fqn, "column_name": None,
            "business_label": None, "score": score,
            "table_business_name": dictionary.get("business_name"),
            "entity_name": entity_row.get("entity"),
            "table_object_type": table_object_type,
            "domain_name": domain_row.get("domain"),
            "relationship_related_tables": relationship_related_tables,
            "name_score": name_score,
            "authority_bonus": total_bonus,
            "ranking_reasons": ranking_reasons,
            "is_approved": bool(ctx.get("dictionary", {}).get("is_approved")) if ctx.get("dictionary") else False,
            "data_type": None,
            "generated_vocabulary_term": generated_vocab_bonus["generated_term"],
        })
    if not candidates:
        return None
    candidates.sort(key=lambda c: -_rank_key(c))
    top = candidates[0]
    top_key = _rank_key(top)
    runner_up_key = _rank_key(candidates[1]) if len(candidates) > 1 else 0.0
    selected = None
    if top_key >= _AUTO_SELECT_MIN_CONFIDENCE and (top_key - runner_up_key) >= _AMBIGUITY_MARGIN:
        selected = top
    return {"selected": selected, "candidates": candidates[:5]}


# ---------------------------------------------------------------------------
# Milestone Phase 6.2 — Aggregation Shape Correctness
#
# "How many students are enrolled?" must count STUDENT RECORDS, not sum/
# count a pre-aggregated metric column whose name happens to overlap the
# term (e.g. ADF_ClassPositionAnalytics.TotalStudents). Entity-count
# questions (aggregation_target in {entity_count, distinct_entity_count} —
# see core.semantic.concept_resolver.extract_query_intent) are routed
# through _resolve_entity_count instead of column-level _resolve_term;
# measure_sum/average/min/max keep the unchanged _resolve_term path, so
# existing SUM/AVG/MIN/MAX behavior is untouched by this section.
# ---------------------------------------------------------------------------

_ENTITY_KEY_TIER_CONFIDENCE = {1: "high", 2: "high", 3: "medium", 4: "weak"}
_ENTITY_KEY_TIER_REASON = {
    1: "declared primary key",
    2: "approved dictionary business identifier",
    3: "high-confidence profiling key candidate (uniqueness_score >= 0.99, identity column)",
    4: "unapproved dictionary business identifier (weakest fallback signal)",
}


def _select_entity_key(ctx: dict) -> dict | None:
    """
    Choose a reliable entity key for COUNT(key)/COUNT(DISTINCT key) from a
    resolved table's already-loaded column metadata (no new reads), in
    priority order:
      1. declared primary key       (profiling.is_primary_key)
      2. approved unique key        (dictionary.is_id AND dictionary.is_approved)
      3. high-confidence profiling key candidate
         (profiling.uniqueness_score >= 0.99 AND profiling.is_identity —
         the same threshold core.profiling.classification.column_typer's
         own ID scorer already uses, reused rather than reinvented)
      4. governed business identifier, unapproved (dictionary.is_id alone)
         — the WEAKEST fallback signal. _resolve_entity_count never lets a
         tier-4 key control join fan-out (see there for why); it is only
         ever used for a safe, no-join COUNT.

    Never returns a PII-flagged column (pii_name_heuristic or pii_risk) —
    there is no existing governance flag for a "safe aggregation" override
    on a PII column, so none is invented here; PII columns are simply
    excluded from key candidacy, full stop. Returns None when nothing
    clears any tier — the caller falls back to bare COUNT(*).
    """
    candidates: list[tuple[int, str]] = []
    for col in ctx.get("columns", []):
        dic = col.get("dictionary") or {}
        prof = col.get("profiling") or {}
        schema = col.get("schema") or {}

        if (prof and prof.get("pii_name_heuristic")) or (dic and dic.get("pii_risk")):
            continue

        if schema and schema.get("is_primary_key"):
            candidates.append((1, col["column_name"]))
        elif dic.get("is_id") and dic.get("is_approved"):
            candidates.append((2, col["column_name"]))
        elif (prof.get("uniqueness_score") or 0) >= 0.99 and schema and schema.get("is_identity"):
            candidates.append((3, col["column_name"]))
        elif dic.get("is_id"):
            candidates.append((4, col["column_name"]))

    if not candidates:
        return None

    tier, col_name = min(candidates, key=lambda c: c[0])
    return {
        "column_name": col_name,
        "tier":        tier,
        "confidence":  _ENTITY_KEY_TIER_CONFIDENCE[tier],
        "reason":      _ENTITY_KEY_TIER_REASON[tier],
    }


def _resolve_entity_count(
    term: str, table_contexts: dict[str, dict], *, distinct_requested: bool,
    concept_mappings: dict[str, list[dict]] | None = None,
    generated_vocab: dict[str, list[dict]] | None = None,
    grounding: dict | None = None,
) -> dict:
    """
    Resolve an entity-count term ("how many students", "number of clients")
    to a counted table + entity key. Reuses _resolve_count_all's existing
    table-name ranking unchanged (never invents a table); only key
    selection and the resulting shape are new. Mirrors _resolve_term's
    return shape (term/selected/candidates/warnings) so build_sql_plan's
    existing unresolved/ambiguous handling applies unchanged.

    Day 2C, Task 4 — when `term` names a grounded target entity, counting
    is narrowed to that entity's own canonical table first
    (_narrow_to_grounded_table), same as _resolve_concept/_resolve_term.
    """
    table_contexts = _narrow_to_grounded_table(term, table_contexts, grounding)
    count_all = _resolve_count_all(term, table_contexts, concept_mappings, generated_vocab)
    if count_all is None:
        return {
            "term": term, "selected": None, "candidates": [],
            "warnings": [{
                "type": "missing_measure", "severity": "MEDIUM",
                "message": f"No entity-count candidate found for '{term}'.",
            }],
        }

    if count_all["selected"] is None:
        candidates = count_all["candidates"]
        return {
            "term": term, "selected": None, "candidates": candidates,
            "warnings": [{
                "type": "ambiguous_measure", "severity": "MEDIUM",
                "message": (
                    f"Multiple entity-count candidates for '{term}' with similar confidence; "
                    "no single authoritative table was auto-selected."
                    if len(candidates) > 1 else
                    f"No confident entity-count match for '{term}'."
                ),
            }],
        }

    selected = dict(count_all["selected"])
    key = _select_entity_key(table_contexts[selected["table_fqn"]])

    if key:
        selected["column_name"] = key["column_name"]
        selected["key_tier"] = key["tier"]
        selected["key_confidence"] = key["confidence"]
        selected["key_selection_reason"] = (
            f"{'COUNT(DISTINCT ' if distinct_requested else 'COUNT('}{key['column_name']}) — {key['reason']}."
        )
        # DISTINCT is only rendered here when the question itself asked for
        # it ("distinct students"); join-fan-out-driven DISTINCT is decided
        # later in plan_business_query, once join_plan is known, and only
        # ever promoted for a tier <= 3 (trusted) key — see there.
        selected["distinct"] = bool(distinct_requested)
    else:
        selected["key_tier"] = None
        selected["key_confidence"] = "none"
        selected["key_selection_reason"] = (
            "No reliable entity key found — using COUNT(*) on the resolved authoritative table."
        )
        selected["distinct"] = False

    selected["aggregation_target"] = "distinct_entity_count" if distinct_requested else "entity_count"

    return {
        "term": term, "selected": selected,
        "candidates": count_all["candidates"], "warnings": [],
    }


# ---------------------------------------------------------------------------
# Milestone Phase 6.6 — Enterprise Clarification Intelligence
#
# When an entity-count question is ambiguous ("how many clients" across
# several tied tables), the orchestration layer asks the user to pick one of
# _resolve_entity_count's own already-ranked candidates instead of guessing.
# This is the resume path: it applies the exact same post-selection
# enrichment (_select_entity_key -> key tier -> COUNT/COUNT(DISTINCT)) that
# _resolve_entity_count already applies to its own auto-selected winner, to
# a caller-chosen candidate instead. No new ranking or scoring — `candidate`
# must already be one of the candidates _resolve_count_all/_resolve_entity_count
# itself produced.
# ---------------------------------------------------------------------------

def enrich_entity_count_selection(
    source_id: int, user_id: str, candidate: dict, *, distinct_requested: bool = False,
) -> dict:
    """Add entity-key enrichment to a user-forced entity-count candidate."""
    selected = dict(candidate)
    ctx = get_table_business_context(source_id, user_id, candidate["table_fqn"])
    key = _select_entity_key(ctx) if ctx else None

    if key:
        selected["column_name"] = key["column_name"]
        selected["key_tier"] = key["tier"]
        selected["key_confidence"] = key["confidence"]
        selected["key_selection_reason"] = (
            f"{'COUNT(DISTINCT ' if distinct_requested else 'COUNT('}{key['column_name']}) — {key['reason']}."
        )
        selected["distinct"] = bool(distinct_requested)
    else:
        selected["key_tier"] = None
        selected["key_confidence"] = "none"
        selected["key_selection_reason"] = (
            "No reliable entity key found — using COUNT(*) on the resolved authoritative table."
        )
        selected["distinct"] = False

    selected["aggregation_target"] = "distinct_entity_count" if distinct_requested else "entity_count"
    return selected


def _apply_join_fanout_safety(measures: list[dict], join_plan: dict, warnings: list[dict]) -> None:
    """
    Join fan-out safety for entity-count measures. A one-to-many join can
    multiply each counted entity across duplicate rows. Only a tier <= 3
    (trusted: declared PK / approved dictionary ID / high-confidence
    profiling key) entity key is trusted to correct that via
    COUNT(DISTINCT key) — an unapproved dictionary.is_id (tier 4) is the
    weakest fallback signal and is never trusted to control fan-out; with
    no key at all there is nothing to de-duplicate on either way. Both
    cases refuse (clear the entry's selection) rather than risk a silently
    inflated count. Mutates `measures` entries and `warnings` in place;
    a no-op for any entry that isn't an entity-count measure, and a no-op
    entirely when the join carries no MEDIUM/HIGH fan-out risk.
    """
    worst_fanout = join_plan.get("fanout_risk")
    if not (join_plan.get("required") and worst_fanout in ("MEDIUM", "HIGH")):
        return

    for entry in measures:
        sel = entry.get("selected")
        if not sel or sel.get("aggregation_target") not in ("entity_count", "distinct_entity_count"):
            continue
        key_tier = sel.get("key_tier")
        if sel.get("column_name") and key_tier is not None and key_tier <= 3:
            sel["distinct"] = True
            sel["key_selection_reason"] += (
                f" Forced COUNT(DISTINCT) — join carries {worst_fanout} fan-out risk and this "
                f"key (tier {key_tier}, confidence={sel['key_confidence']}) is trusted to de-duplicate."
            )
        else:
            reason = (
                "the only available key is an unapproved dictionary identifier "
                "(weakest fallback signal) and cannot be trusted to control it"
                if key_tier == 4 else
                "no reliable entity key was found to de-duplicate on"
            )
            message = (
                f"Counting '{entry['term']}' requires a join with {worst_fanout} fan-out risk, but "
                f"{reason} — refusing to return a potentially inflated count."
            )
            entry["selected"] = None
            fanout_warning = {
                "type": "uncontrolled_fanout_entity_count", "severity": "HIGH", "message": message,
            }
            entry["warnings"].append(fanout_warning)
            warnings.append(fanout_warning)


# ---------------------------------------------------------------------------
# Milestone M-4 — Enterprise Semantic Resolution
#
# Resolves a bare business-concept term ("clients", "students" — the request's
# `concepts` list) directly to an authoritative table, the same way
# _resolve_count_all resolves a bare COUNT(*) term: same name-match +
# _score_table_authority scoring, same _AUTO_SELECT_MIN_CONFIDENCE/
# _AMBIGUITY_MARGIN auto-select gate. Unlike _resolve_count_all, the
# candidate/selected shape here surfaces the full business context
# get_table_business_context() already assembled for this table (business
# description, domain, entity, governance, relationship coverage) instead of
# a bare column-shaped dict — this is the "semantic context" the concept
# resolved into, and why it won, in one object. No new reads, no invented
# metadata: every field below is already present in `ctx`.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Day 2A, Task 2 — Trust- and Specificity-Aware Entity Selection.
#
# Root cause (verified against real CCPP metadata for "Active invoices"):
# _score_term_match tries every governed synonym expansion of a term
# (vocabulary_service.expand_concept) and keeps the BEST score across all of
# them, so a short, generic table name that happens to equal one synonym of
# the term can reach a perfect name_score=1.0 via an exact string match,
# while a longer, more descriptive table name that is the term's real,
# specific business object only partially token-overlaps and scores much
# lower. When the synonym-matched table is ungoverned (no dictionary
# approval, no domain assignment), that perfect-but-coincidental name_score
# can still silently outrank a governed, more specific candidate.
#
# This guard runs once per already-scored candidate list, after the caller
# builds it and before it is sorted/auto-selected. It is COMPARATIVE, never
# a blanket penalty:
#   - an ungoverned candidate with no governed competitor at all is left
#     exactly as scored (a lone synonym-matched candidate is still useful —
#     "trusted curated vocabulary may still win when appropriate");
#   - two governed candidates are never touched by this guard — it only
#     ever discounts an UNGOVERNED top candidate, so a genuine tie between
#     two governed systems still falls through to clarification exactly as
#     before;
#   - the discount is exactly the gap between the top candidate's
#     vocabulary-assisted name_score and its OWN literal (non-expanded)
#     specificity to the term — never a fixed/tuned magnitude, and never
#     keyed off any specific term or table name.
#
# Never reads/writes _AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN — the
# existing auto-select/ambiguity gate is completely unchanged; this only
# adjusts the candidate's own authority_bonus (the same additive field
# _rank_key already sums with name_score), so a governed candidate that
# still isn't genuinely competitive after the adjustment correctly falls
# through to the same clarification path as before instead of being forced
# to auto-select.
# ---------------------------------------------------------------------------

_TRUST_GUARD_SPECIFICITY_GAP = 0.15


def _is_governed_candidate(candidate: dict) -> bool:
    """Governed = has real curatorial evidence behind it (an approved
    dictionary entry or a real, non-"Unknown" domain assignment) — the same
    governance fields _score_table_authority already reads, never a new
    signal."""
    governance = candidate.get("governance") or {}
    return bool(governance.get("dictionary_approved") or governance.get("domain_assigned"))


def _literal_specificity_score(term: str, candidate: dict) -> float:
    """How much of the candidate's OWN name/business-name is literally
    explained by `term`, WITHOUT any synonym/vocabulary expansion — reuses
    _score_term_match_single (single literal term, no expand_concept), so a
    full business-phrase match against a descriptive table name scores
    higher than a short, generic table name that only matches via a
    synonym substitution.
    """
    table_name = candidate["table_fqn"].split(".")[-1]
    return _score_term_match_single(term, table_name, candidate.get("business_name"))


def _apply_trust_specificity_guard(candidates: list[dict], term: str) -> list[dict]:
    """Reusable ranking guard — operates on any candidate list shaped like
    _resolve_concept's own (table_fqn/business_name/governance/name_score/
    authority_bonus/score). Candidates are adjusted in place; the same list
    is returned for the caller to sort as usual."""
    if len(candidates) < 2:
        return candidates

    if not any(_is_governed_candidate(c) for c in candidates):
        return candidates  # nothing governed competes — leave scoring as-is

    top = max(candidates, key=_rank_key)
    if _is_governed_candidate(top):
        return candidates  # never arbitrate between/around governed candidates

    literal_specificity = _literal_specificity_score(term, top)
    vocabulary_advantage = top["name_score"] - literal_specificity
    if vocabulary_advantage <= _TRUST_GUARD_SPECIFICITY_GAP:
        return candidates  # top candidate's match is genuinely specific, not vocabulary-inflated

    top["authority_bonus"] = top["authority_bonus"] - vocabulary_advantage
    top["score"] = max(0.0, min(1.0, top["name_score"] + top["authority_bonus"]))
    top["ranking_reasons"] = list(top.get("ranking_reasons") or []) + [
        "Trust/specificity guard: this candidate is unapproved with no domain assignment, and "
        "its match relies mainly on vocabulary/synonym expansion rather than literal specificity "
        f"to the term (discount {vocabulary_advantage:.3f})."
    ]
    return candidates


# ---------------------------------------------------------------------------
# Day 2C, Task 4 — Role-Selection Control.
#
# Generalizes the existing status_hints precedence pattern (below, in
# plan_business_query's status-filter resolution) to table-role selection:
# a grounded target entity's own canonical table already cleared Day 2C's
# deterministic validation (data.semantic_contract_service.
# validate_candidate_contract) before it was ever persisted, so when a term
# literally names that entity, its canonical table should control which
# table plays that role — not get re-litigated against every other table in
# a broader candidate pool via generic authority/generated-vocabulary
# scoring. A fully-grounded question already gets this for free (the whole
# broad search is skipped, query_planning_service.py's own seed_table_fqns-
# only candidate pool above) — this is what makes the SAME guarantee hold
# per-term under PARTIAL multi-entity grounding, where the broader pool
# still contains other entities'/broad-search tables a generated-vocabulary
# bonus could otherwise let win.
# ---------------------------------------------------------------------------

def _grounded_trusted_tables_for_term(term: str, grounding: dict | None) -> "set[str] | None":
    """If `term` itself names one of the fixed target entities
    (data.semantic_contract_service.ENTITY_TAXONOMY) AND that entity is
    grounded (RESOLVED/PARTIAL contract with a verified canonical table —
    apply_grounding's own "grounded" criterion, unchanged here), returns
    that entity's own trusted table set (canonical table + its contract's
    own alternate_candidates — NOT other entities' tables); otherwise None.

    Includes alternates deliberately: Day 2A Task 4's bounded status-retry
    (_resolve_status_compatible_entity) needs a real second candidate for
    THIS SAME entity to retry into when the canonical table itself lacks a
    compatible status field — narrowing to the canonical table alone would
    silently defeat that mechanism (confirmed by a real regression: see
    tests/test_status_resolution_bounded_retry.py's end-to-end case).

    Pure — grounding is already-computed data passed in, no DB access.

    First tries the original whole-term match (match_entity_for_terms(term,
    [term], [term], [])) — correct as-is for a single-word entity, and for
    a term extract_terms() kept as the full multi-word phrase itself (e.g.
    "job orders" surviving intact). Falls back to checking each of
    grounding's OWN already-matched entities' synonym WORDS individually
    only when that whole-term match finds nothing: a multi-word entity name
    ("Launch Participant") can also be split by extract_terms() into
    separate single-word tokens ("launch", "participants"), and neither
    token alone can re-match the multi-word synonym via the whole-term path
    — that call's "question" text IS just the single token, so a two-word
    synonym can never be found in it. That silently returned None for every
    such token, leaving concept resolution unnarrowed (scored against the
    full candidate pool, including this very entity's own
    alternate_candidates) for exactly the entities most likely to be split
    into multiple terms. The word-level fallback only ever matches against
    entities grounding has ALREADY matched against the full question, so it
    can't introduce a false match a whole-question check wouldn't also make.
    """
    if not grounding or not grounding.get("entity_table_map"):
        return None
    from data.semantic_contract_service import match_entity_for_terms, ENTITY_TAXONOMY
    entity_name = match_entity_for_terms(term, [term], [term], [])
    if entity_name:
        return grounding.get("entity_trusted_tables", {}).get(entity_name)
    term_lower = (term or "").lower()
    for entity_name in grounding["entity_table_map"]:
        for synonym in ENTITY_TAXONOMY.get(entity_name, ()):
            words = synonym.split()
            if len(words) > 1 and any(_tokens_near_match(term_lower, word) for word in words):
                return grounding.get("entity_trusted_tables", {}).get(entity_name)
    return None


def _narrow_to_grounded_table(
    term: str, table_contexts: dict[str, dict], grounding: dict | None,
) -> dict[str, dict]:
    """When `term` names a grounded target entity, restrict resolution to
    that entity's own trusted table set (canonical + its own alternates)
    that are ALSO present in this call's own candidate pool — the per-term
    equivalent of the fully-grounded broad-search skip above. An unrelated
    broad-search table (a different entity's table, or generic-vocabulary
    noise) never gets a chance to outscore the grounded entity for its own
    role, since it is excluded from scoring entirely; a real alternate for
    the SAME entity is preserved so Day 2A's bounded status-retry still has
    something to retry into. Purely additive/narrowing: never touched when
    the term doesn't name a grounded entity, or when NONE of that entity's
    trusted tables are even in this call's own table_contexts (nothing
    real to narrow to — falls through to the unmodified broad pool rather
    than risk narrowing to empty).

    PARTIAL contracts participate identically to RESOLVED ones here — Task
    1/2 already verified canonical-object identity/grain/key before either
    was persisted; PARTIAL only means status values weren't verified, which
    is an orthogonal concern this table-role decision doesn't touch.
    """
    trusted = _grounded_trusted_tables_for_term(term, grounding)
    if not trusted:
        return table_contexts
    narrowed = {fqn: ctx for fqn, ctx in table_contexts.items() if fqn in trusted}
    return narrowed or table_contexts


# Day 2E, Task 1 — a generic status/attribute word never names an entity
# itself, so entity_consumed_terms() correctly never claims it; this is the
# separate, small vocabulary of words that DO ask for a grounded entity's
# status/current-state attribute rather than the entity itself. Kept tiny
# and generic on purpose — no entity or table name here, ever.
_STATUS_ATTRIBUTE_TERMS = frozenset({"status", "statuses", "current status", "current_status"})


def _term_names_grounded_entity(term_lower: str, entity_name: str) -> bool:
    from data.semantic_contract_service import ENTITY_TAXONOMY
    for synonym in ENTITY_TAXONOMY.get(entity_name, ()):
        if term_lower == synonym:
            return True
        if any(_tokens_near_match(term_lower, word) for word in synonym.split()):
            return True
    return False


def _resolve_entity_role_from_contract(term: str, grounding: dict | None) -> dict | None:
    """Day 2E, Task 1 — Contract Authority. When `term` names one of
    grounding's own GROUNDED (RESOLVED/PARTIAL, verified canonical table)
    entities, or is a status/attribute word for one, returns the contract's
    OWN object for that role directly — never a candidate to be scored
    against generated vocabulary, lexical similarity, table authority, or
    the entity's own alternate_candidates. A verified contract already IS
    the answer to "what table represents this," not one more entry in a
    naming contest; that contest is exactly what previously let a stale
    historical alternate (kept only for Day 2A's bounded status-retry) win
    over the real canonical table on lexical score alone.

    Role precedence:
      1. `term` names the entity itself (whole synonym phrase, or one of
         its words — covers both an intact multi-word term and a token
         extract_terms() split apart) -> the entity's canonical_table_fqn.
      2. `term` is a generic status/attribute word and the entity has a
         preferred_view_fqn (its verified status/analytical alternate) ->
         that view; falls back to the canonical table if the entity has no
         dedicated view (still a real, contract-declared object, never an
         unrelated table).
      3. Otherwise -> None; the caller falls back to the existing narrowed
         (never unnarrowed) scoring path.

    Pure — grounding is already-computed data passed in, no DB access. No
    entity/table name is ever literal here — every object comes off the
    grounding dict dynamically.
    """
    if not grounding or not grounding.get("grounded"):
        return None
    term_lower = (term or "").lower().strip()
    if not term_lower:
        return None
    for entity_name, contract in grounding["grounded"].items():
        if _term_names_grounded_entity(term_lower, entity_name):
            canonical = contract.get("canonical_table_fqn")
            if canonical:
                return {"role": "canonical", "table_fqn": canonical, "entity_name": entity_name}
        if term_lower in _STATUS_ATTRIBUTE_TERMS:
            target = contract.get("preferred_view_fqn") or contract.get("canonical_table_fqn")
            if target:
                return {"role": "status", "table_fqn": target, "entity_name": entity_name}
    return None


def _resolve_contract_status_column(term: str, kind: str, table_fqn: str, ctx: dict) -> dict:
    """The column-level half of the "status" role above: a status/attribute
    word is resolved by name/label against the ONE contract-approved table's
    own real columns (_find_status_column — the same evidence-ranked status-
    column detector the status-FILTER path already trusts), never scored
    against any predicate-filtered pool across multiple tables. No match on
    that single table is a genuine, reportable failure — never a silent
    fallback to an unrelated global table."""
    status_col = _find_status_column({table_fqn: ctx}, table_fqn)
    if not status_col:
        return {
            "term": term, "selected": None, "candidates": [],
            "warnings": [{
                "type": f"missing_{kind}", "severity": "MEDIUM",
                "message": f"'{term}' names a verified data source with no compatible status column.",
            }],
            "semantic_compatibility": None,
        }
    fqn, column_name = status_col
    col_ctx = next((c for c in ctx.get("columns", []) if c["column_name"] == column_name), None)
    dic = (col_ctx or {}).get("dictionary") or {}
    selected = {"table_fqn": fqn, "column_name": column_name, "business_label": dic.get("business_label"), "score": 1.0}
    return {"term": term, "selected": selected, "candidates": [selected], "warnings": [], "semantic_compatibility": None}


def _build_concept_candidate(
    term: str, table_fqn: str, ctx: dict,
    mapping_lookup: dict | None, vocab_lookup: dict | None,
) -> dict:
    """The per-table candidate-dict construction _resolve_concept's own
    scoring loop uses — factored out so Day 2E's contract-authority
    shortcut (a single, pre-decided table) can build the SAME real-metadata
    shaped candidate (business_name/domain/governance/etc. all genuine,
    not invented) without re-running the multi-table scoring contest this
    function's caller uses it for."""
    table_name = table_fqn.split(".")[-1]
    dictionary = ctx.get("dictionary") or {}
    domain = ctx.get("domain") or {}
    entity = ctx.get("entity") or {}
    relationships = ctx.get("relationships") or {}
    name_score = _score_term_match(term, table_name, dictionary.get("business_name"), dictionary.get("description"))
    authority = _score_table_authority(table_fqn, ctx, name_score)
    concept_mapping = _concept_mapping_bonus(mapping_lookup, term, table_fqn, None)
    generated_vocab_bonus = _generated_vocabulary_bonus(
        vocab_lookup, term, table_fqn, None, concept_bonus_active=bool(concept_mapping["bonus"]),
    )
    total_bonus = authority["bonus"] + concept_mapping["bonus"] + generated_vocab_bonus["bonus"]
    score = max(0.0, min(1.0, name_score + total_bonus))
    ranking_reasons = list(authority["reasons"])
    if concept_mapping["reason"]:
        ranking_reasons.append(concept_mapping["reason"])
    if generated_vocab_bonus["reason"]:
        ranking_reasons.append(generated_vocab_bonus["reason"])
    return {
        "table_fqn":               table_fqn,
        "business_name":           dictionary.get("business_name"),
        "business_description":    dictionary.get("description"),
        "domain":                  domain.get("domain"),
        "entity":                  entity.get("entity"),
        "is_approved":             bool(dictionary.get("is_approved")),
        "governance":              ctx.get("governance"),
        "relationships_summary": {
            "outbound_count": len(relationships.get("outbound") or []),
            "inbound_count":  len(relationships.get("inbound") or []),
        },
        "score":            score,
        "name_score":       name_score,
        "authority_bonus":  total_bonus,
        "ranking_reasons":  ranking_reasons,
        "generated_vocabulary_term": generated_vocab_bonus["generated_term"],
    }


def _resolve_concept(
    term: str, table_contexts: dict[str, dict],
    concept_mappings: dict[str, list[dict]] | None = None,
    generated_vocab: dict[str, list[dict]] | None = None,
    grounding: dict | None = None,
) -> dict:
    # Day 2E, Task 1 — Contract Authority. A term naming a fully grounded
    # entity is resolved directly to the contract's own canonical table —
    # never scored against its own alternate_candidates or anything else in
    # the pool. See _resolve_entity_role_from_contract's own docstring.
    contract_hit = _resolve_entity_role_from_contract(term, grounding)
    if contract_hit and contract_hit["role"] == "canonical" and contract_hit["table_fqn"] in table_contexts:
        table_fqn = contract_hit["table_fqn"]
        mapping_lookup = _concept_mapping_lookup(concept_mappings, term)
        vocab_lookup = _generated_vocab_lookup(generated_vocab, term)
        # Trusted alternates stay in `candidates` (never in the running for
        # `selected`, which is forced to canonical below) purely so Day 2A's
        # bounded status-retry (_resolve_status_compatible_entity) still has
        # a real second candidate to retry into when the canonical table
        # itself turns out to lack a compatible status field for THIS
        # question — that mechanism reads candidates[1:], not the pool.
        trusted = _grounded_trusted_tables_for_term(term, grounding) or {table_fqn}
        pool = {fqn: table_contexts[fqn] for fqn in trusted if fqn in table_contexts}
        pool.setdefault(table_fqn, table_contexts[table_fqn])
        candidates = [_build_concept_candidate(term, fqn, ctx, mapping_lookup, vocab_lookup) for fqn, ctx in pool.items()]
        candidate = next(c for c in candidates if c["table_fqn"] == table_fqn)
        candidate["score"] = 1.0
        candidates.sort(key=lambda c: 0 if c["table_fqn"] == table_fqn else 1)
        return {
            "term": term, "resolved": True, "selected": candidate,
            "candidates": candidates[:5], "confidence": 1.0, "ambiguity_reason": None,
        }

    table_contexts = _narrow_to_grounded_table(term, table_contexts, grounding)
    mapping_lookup = _concept_mapping_lookup(concept_mappings, term)
    vocab_lookup = _generated_vocab_lookup(generated_vocab, term)
    candidates: list[dict] = [
        _build_concept_candidate(term, table_fqn, ctx, mapping_lookup, vocab_lookup)
        for table_fqn, ctx in table_contexts.items()
    ]

    candidates = _apply_trust_specificity_guard(candidates, term)
    candidates.sort(key=lambda c: -_rank_key(c))

    selected = None
    ambiguity_reason = None
    if not candidates:
        ambiguity_reason = f"No candidate table found for concept '{term}'."
    else:
        top = candidates[0]
        top_key = _rank_key(top)
        runner_up_key = _rank_key(candidates[1]) if len(candidates) > 1 else 0.0
        if top_key >= _AUTO_SELECT_MIN_CONFIDENCE and (top_key - runner_up_key) >= _AMBIGUITY_MARGIN:
            selected = top
        else:
            ambiguity_reason = (
                f"Multiple candidate tables for concept '{term}' with similar confidence; "
                "no single authoritative table was auto-selected."
                if len(candidates) > 1 else
                f"No confident match for concept '{term}' "
                f"(best score {top['score']:.2f} below the {_AUTO_SELECT_MIN_CONFIDENCE:.2f} threshold)."
            )

    return {
        "term":             term,
        "resolved":         selected is not None,
        "selected":         selected,
        "candidates":       candidates[:5],
        "confidence":       selected["score"] if selected else (candidates[0]["score"] if candidates else 0.0),
        "ambiguity_reason": ambiguity_reason,
    }


# ---------------------------------------------------------------------------
# Calendar-grain dimension fallback ("each year" / "by month" / "per year")
#
# No column is ever literally named "year"/"month"/"quarter"/"week"/"day",
# so ordinary _resolve_term name-match column search always leaves a bare
# calendar-grain dimension term unresolved. Only used as a fallback AFTER
# _resolve_term already found nothing confident for the term — never
# overrides a genuine column match, and never invents a table: the candidate
# pool is restricted to the single already-resolved primary table's own real
# date/datetime columns.
# ---------------------------------------------------------------------------

_CALENDAR_GRAIN_TERMS = frozenset({"year", "month", "quarter", "week", "day"})


def _is_date_column(col: dict) -> bool:
    dic = col.get("dictionary") or {}
    prof = col.get("profiling") or {}
    return bool(dic.get("is_date")) or prof.get("semantic_type") == "date"


def _resolve_calendar_grain_dimension(
    grain_term: str, table_fqn: str, ctx: dict, hint_terms: list[str],
) -> dict:
    """
    Resolve a bare calendar-grain dimension term ("year", "month", ...) to a
    real DATE/DATETIME column on the already-resolved primary table.

    When the table has more than one date column (e.g. StartDate, EndDate,
    InsertDate), the other, non-grain words already extracted from the same
    question (e.g. "started" for "students STARTED each year") disambiguate
    via the same _score_term_match token-overlap scoring used everywhere
    else in this module — never a guess: ties (or no hint match at all) fall
    back to the table's own column order deterministically.
    """
    date_cols = [c for c in (ctx.get("columns") or []) if _is_date_column(c)]
    if not date_cols:
        return {
            "term": grain_term, "selected": None, "candidates": [],
            "warnings": [{
                "type": "missing_dimension", "severity": "MEDIUM",
                "message": f"No date column found on {table_fqn} to group by {grain_term}.",
            }],
            "semantic_compatibility": None,
        }

    def _hint_score(col: dict) -> float:
        dic = col.get("dictionary") or {}
        best = 0.0
        for hint in hint_terms:
            best = max(best, _score_term_match(hint, col["column_name"], dic.get("business_label")))
        return best

    ranked = sorted(enumerate(date_cols), key=lambda pair: (-_hint_score(pair[1]), pair[0]))
    winner = ranked[0][1]
    candidates = [
        {
            "table_fqn": table_fqn, "column_name": c["column_name"],
            "business_label": (c.get("dictionary") or {}).get("business_label"),
            "score": _hint_score(c),
        }
        for _, c in ranked[:5]
    ]
    return {
        "term": grain_term,
        "selected": {
            "table_fqn": table_fqn,
            "column_name": winner["column_name"],
            "business_label": (winner.get("dictionary") or {}).get("business_label"),
            "score": 1.0,
            "time_grain": grain_term,
        },
        "candidates": candidates,
        "warnings": [],
        "semantic_compatibility": None,
    }


# ---------------------------------------------------------------------------
# Related-table attribute fallback ("names of the students")
#
# A bare attribute term ("names") that names no column on the already-
# resolved concept table itself may still name a real attribute of a table
# the concept table has a declared, TRUSTED (AUTO/APPROVED) relationship to
# (e.g. dbo.ADF_Student.StudentUserID -> dnnuser.Users.UserID). Never invents
# a relationship or a column: reuses the same trusted-relationship discovery
# join planning already uses (analyze_join_quality) and only auto-selects a
# column whose own dictionary metadata confirms it is name-shaped.
# ---------------------------------------------------------------------------

_NAME_ATTRIBUTE_TERMS = frozenset({"name", "names"})

# Phase 1 Stabilization — entity-relative attribute resolution, Tier 3
# ("semantic role compatibility"). A relationship whose FK column is itself
# near-unique on the concept table (analyze_join_quality's own cardinality
# ONE_TO_ONE/ONE_TO_MANY, already computed from profiling_column_profiles.
# uniqueness_score — no new metadata) means each concept row maps to its OWN
# distinct related row: an identity-style link (e.g. Student -> its own
# User account). MANY_TO_ONE/MANY_TO_MANY/UNKNOWN means many concept rows
# share the same related row: a category/lookup-style link (e.g. many
# Students -> one shared Marketing campaign) — structurally never the
# source of a person's own name, regardless of how strongly a column's
# NAME happens to lexically match "name" (a column called "MarketingName"
# scores just as high on lexical similarity alone as "FirstName" does).
# This is a generic structural signal — no table/column names are special-
# cased — that lets the identity-style relationship dominate the tie
# instead of lexical similarity deciding it.
_IDENTITY_CARDINALITIES = frozenset({"ONE_TO_ONE", "ONE_TO_MANY"})
_ROLE_COMPATIBLE_BONUS = 0.5


def _resolve_related_attribute_dimension(
    term: str, concept_table_fqn: str, source_id: int, user_id: str,
) -> list[dict] | None:
    """Returns None (never a synthetic refusal entry) when the term isn't a
    recognized attribute keyword, or the concept table has no context — the
    caller keeps the original, ordinary _resolve_term result in that case.

    Returns a LIST of resolved dimension entries, not a single one: several
    near-tied name-like columns on the SAME related table (FirstName +
    LastName) are complementary parts of "name", not competing alternatives
    — every one of them is selected (capped at 3), each as its own entry, so
    sql_planning_service's ordinary one-entry-per-select-row contract needs
    no changes. A tie ACROSS different tables, after weighting by semantic
    role compatibility (see _IDENTITY_CARDINALITIES above), is genuine
    ambiguity and stays unresolved (single entry, selected=None) rather than
    guessing.
    """
    if term.lower() not in _NAME_ATTRIBUTE_TERMS:
        return None
    ctx = get_table_business_context(source_id, user_id, concept_table_fqn)
    if not ctx:
        return None

    candidates: list[dict] = []
    for related_fqn in _related_table_names(ctx, concept_table_fqn):
        join = analyze_join_quality(source_id, user_id, concept_table_fqn, related_fqn)
        best_join = (join or {}).get("best_join")
        if not best_join:
            continue  # no trusted (AUTO/APPROVED) join path — never guess a join
        role_bonus = _ROLE_COMPATIBLE_BONUS if best_join.get("cardinality") in _IDENTITY_CARDINALITIES else 0.0
        related_ctx = get_table_business_context(source_id, user_id, related_fqn)
        if not related_ctx:
            continue
        for col in related_ctx.get("columns") or []:
            dic = col.get("dictionary") or {}
            name_score = _score_term_match(term, col["column_name"], dic.get("business_label"))
            if name_score >= _AUTO_SELECT_MIN_CONFIDENCE:
                candidates.append({
                    "table_fqn": related_fqn, "column_name": col["column_name"],
                    "business_label": dic.get("business_label"),
                    "score": name_score + role_bonus,
                    "cardinality": best_join.get("cardinality"),
                })

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c["score"])
    top = candidates[0]
    distinct_tables = {c["table_fqn"] for c in candidates}
    other_table_runner_up = next((c["score"] for c in candidates if c["table_fqn"] != top["table_fqn"]), 0.0)

    if len(distinct_tables) > 1 and (top["score"] - other_table_runner_up) < _AMBIGUITY_MARGIN:
        return [{
            "term": term, "selected": None, "candidates": candidates[:5],
            "warnings": [{
                "type": "ambiguous_dimension", "severity": "MEDIUM",
                "message": f"Multiple name-like columns found on related tables for '{term}'.",
            }],
            "semantic_compatibility": None,
        }]

    same_table_matches = [c for c in candidates if c["table_fqn"] == top["table_fqn"]]
    return [
        {
            "term": term, "selected": c, "candidates": candidates[:5],
            "warnings": [], "semantic_compatibility": None,
        }
        for c in same_table_matches[:3]
    ]


def _resolve_ranking_order_column(order: dict, measures: list[dict]) -> tuple[dict, dict | None]:
    """
    Resolve a non-date ranking/order intent's target column from the
    query's own measures: rank by the first selected measure's column,
    exactly the rule plan_business_query has always used. Returns
    (resolved_order, warning_or_None) — order.direction/limit are always
    preserved untouched; only table_fqn/column_name are (maybe) added.

    Factored out so core.orchestrator.context_builder._apply_clarification_
    overrides can call this identical rule again once clarification has
    populated measures[].selected for a term that was still ambiguous
    (selected=None) the first time plan_business_query called this —
    one ordering implementation, invoked at two points in time, never two.
    """
    order = dict(order)
    primary_measure = next((m["selected"] for m in measures if m["selected"]), None)
    if primary_measure and primary_measure.get("column_name"):
        order["table_fqn"] = primary_measure["table_fqn"]
        order["column_name"] = primary_measure["column_name"]
        return order, None
    return order, {
        "type": "order_column_not_found", "severity": "LOW",
        "message": "Ranking was requested but no measure/dimension was specified to rank "
                   "by — results are limited but not sorted.",
    }


def _find_date_column(table_contexts: dict[str, dict], preferred_table: str | None) -> tuple[str, str] | None:
    """
    Locate a date column for date-range filtering. Primary signal: dictionary
    is_date flag (already-classified, human-reviewable). Fallback: profiling
    semantic_type == 'DATE' (the real, uppercase stored value — see
    core/profiling/models.py::SemanticType). Never invents a date column that
    wasn't already discovered by dictionary/profiling classification.
    Checks `preferred_table` first, then falls back to any candidate table.
    """
    ordered = ([preferred_table] if preferred_table else []) + [
        t for t in table_contexts if t != preferred_table
    ]
    for table_fqn in ordered:
        ctx = table_contexts.get(table_fqn)
        if not ctx:
            continue
        for col in ctx.get("columns", []):
            dic = col.get("dictionary") or {}
            prof = col.get("profiling") or {}
            if dic.get("is_date") or prof.get("semantic_type") == "DATE":
                return table_fqn, col["column_name"]
    return None


# "Show the 10 most recently ADDED students" must sort by an insert/created
# timestamp (InsertDate), not an unrelated business date on the same table
# (StartDate, EndDate) — _find_date_column's own "first date column found"
# rule has no way to tell those apart when a table has more than one date
# column. These verbs are exactly the words extract_terms() already strips
# as grammatical stopwords ("added", "started", ...) before term
# resolution, so they survive only in the RAW question text — read directly
# from there, never re-added as a business term.
_DATE_ORDER_HINT_ROOTS: dict[str, str] = {
    "added": "insert", "add": "insert", "created": "insert", "create": "insert",
    "inserted": "insert", "insert": "insert", "registered": "insert",
    "started": "start", "start": "start", "began": "start", "begin": "start",
    "ended": "end", "end": "end", "completed": "end", "complete": "end",
    "finished": "end", "finish": "end",
    "updated": "update", "update": "update", "modified": "update", "modify": "update",
}


def _date_order_hint_root(question: str) -> str | None:
    for word in re.findall(r"[a-zA-Z]+", question or ""):
        root = _DATE_ORDER_HINT_ROOTS.get(word.lower())
        if root:
            return root
    return None


def _find_date_column_with_hint(
    table_contexts: dict[str, dict], preferred_table: str | None, preferred_root: str | None,
) -> tuple[str, str] | None:
    """Same candidate discovery as _find_date_column, but when a table has
    more than one date column, prefers one whose name contains
    `preferred_root` (e.g. "insert" for "most recently ADDED") over an
    arbitrary first match. Never invents a column — only re-ranks among the
    same date columns _find_date_column would already consider; falls back
    to its exact behavior when no hint is given or nothing matches it."""
    if not preferred_root:
        return _find_date_column(table_contexts, preferred_table)

    ordered = ([preferred_table] if preferred_table else []) + [
        t for t in table_contexts if t != preferred_table
    ]
    for table_fqn in ordered:
        ctx = table_contexts.get(table_fqn)
        if not ctx:
            continue
        date_cols = [
            col["column_name"] for col in ctx.get("columns", [])
            if (col.get("dictionary") or {}).get("is_date")
            or (col.get("profiling") or {}).get("semantic_type") == "DATE"
        ]
        if not date_cols:
            continue
        hinted = next((c for c in date_cols if preferred_root in c.lower()), None)
        return table_fqn, (hinted or date_cols[0])
    return None


_STATUS_KEYWORDS = {"status", "state", "stage", "active", "inactive", "lifecycle", "disposition"}


def _find_status_column(table_contexts: dict[str, dict], preferred_table: str | None) -> tuple[str, str] | None:
    """
    Locate a status column for status-value filtering. Evidence, in priority
    order, checked per table before moving to the next candidate table:
    (1) profiling semantic_type == 'STATUS' (core/profiling/classification/
    column_typer.py already classifies this); (2) dictionary business_label
    containing a status/lifecycle keyword — dictionary.semantic_type itself
    collapses STATUS into the generic 'dimension' bucket (see
    core/dictionary/generator.py _PROFILING_TO_DICT_SEMANTIC), so
    business_label is the only usable dictionary-level signal; (3) normalized
    column-name token match against the same keyword set — the same
    naming-convention fallback pattern _is_metric_column already uses.
    Never invents a status column.
    """
    ordered = ([preferred_table] if preferred_table else []) + [
        t for t in table_contexts if t != preferred_table
    ]
    for table_fqn in ordered:
        ctx = table_contexts.get(table_fqn)
        if not ctx:
            continue
        for col in ctx.get("columns", []):
            prof = col.get("profiling") or {}
            if prof.get("semantic_type") == "STATUS":
                return table_fqn, col["column_name"]
        for col in ctx.get("columns", []):
            dic = col.get("dictionary") or {}
            label = dic.get("business_label") or ""
            if label and _STATUS_KEYWORDS & set(_tokenize(label)):
                return table_fqn, col["column_name"]
        for col in ctx.get("columns", []):
            if _STATUS_KEYWORDS & set(_tokenize(col["column_name"])):
                return table_fqn, col["column_name"]
    return None


# ---------------------------------------------------------------------------
# Phase 1 Stabilization — Single-Object Sufficiency Check.
#
# "How many launch participants are stalled, active, graduated, or not
# started?" is a grouped-count-by-value-domain request: none of the
# enumerated VALUES ("stalled") lexically match any column NAME
# ("Current_Status"), so ordinary term-to-column resolution can never
# resolve it — it always fell through to an expensive multi-table candidate
# search and then a join/relationship search across every candidate, none
# of which could succeed either (there is no name-matched term to join on),
# ending in a slow "no_valid_join" refusal.
#
# Checked BEFORE any of that runs: when one already-known, already-profiled
# object (a curated VIEW preferred over a raw table) already contains the
# requested entity's grain and a status-like dimension column whose CACHED
# sample values cover every requested literal, the request is answered
# directly against that one object. Reads only already-discovered local
# metadata (dictionary + cached profiling_value_samples) — never opens a
# live connection, so this check itself can never be a source of the
# live-round-trip latency a join/relationship search risks.
# ---------------------------------------------------------------------------

_SUFFICIENCY_CANDIDATE_LIMIT = 10   # metadata retrieval: top 10 candidates
_SUFFICIENCY_INSPECT_LIMIT = 5      # detailed metadata inspection: max 5 objects


def _normalize_value(v) -> str:
    if v is None:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", str(v).lower()).strip()


def _cached_status_value_coverage(
    source_id: int, table_fqn: str, column_name: str, required: list[str], *, session=None,
) -> bool | None:
    """True/False when this column has >=1 cached profiling value sample
    (covers every requested literal, or doesn't); None when it has NEVER
    been sampled at all — a distinct outcome from "sampled but doesn't
    cover," used by the caller to decide whether one bounded live probe is
    warranted (a required value mapping is unknown) vs. a confident no."""
    if not required:
        return False
    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        rows = conn.execute(
            """SELECT DISTINCT pvs.value FROM profiling_value_samples pvs
               JOIN profiling_column_profiles pcp ON pvs.profiling_column_profile_id = pcp.id
               WHERE pcp.source_id = ? AND pcp.table_fqn = ? AND pcp.column_name = ?
                 AND pvs.value IS NOT NULL""",
            (source_id, table_fqn, column_name),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    samples = {_normalize_value(r["value"]) for r in rows}
    if not samples:
        return None
    return all(
        any(_normalize_value(req) == s or _normalize_value(req) in s or s in _normalize_value(req) for s in samples)
        for req in required
    )


# ---------------------------------------------------------------------------
# Day 2A, Task 4 — Status Field Resolution With One Bounded Retry.
#
# _find_status_column (above) already discovers a status-like COLUMN by
# name/label/semantic-type evidence, but never checks whether the actual
# requested VALUE ("active"/"open"/"completed"/...) is a real, typed value
# on that specific column — and the bare-entity path only ever looked at
# the single top-ranked concept candidate's table: if THAT table had no
# compatible status field, the whole question refused instead of trying
# the next credible candidate for the same concept.
# ---------------------------------------------------------------------------

_STATUS_BOOLEAN_VALUES = {"active": 1, "inactive": 0}


def _typed_status_filter_value(
    col_ctx: dict, status_value: str, source_id: int, table_fqn: str, column_name: str, *, session=None,
) -> tuple[str, object] | None:
    """(operator, value) when `status_value` is actually compatible with
    this column's real type/known values, else None:
      - bit/boolean column: only "active"/"inactive" map (to 1/0 respectively)
        — any other status word against a boolean column is incompatible.
      - textual column: the value must not be a CONFIRMED miss against the
        column's own cached sampled values (_cached_status_value_coverage —
        already-collected profiling evidence, no new read/probe). Never
        sampled (unknown coverage) is treated as compatible: there is no
        evidence against it, and this preserves existing behavior for the
        many columns that simply haven't been value-sampled yet, rather
        than turning every one of them into a new refusal.
    """
    data_type = (col_ctx.get("schema") or {}).get("data_type")
    if data_type == "BOOLEAN":
        mapped = _STATUS_BOOLEAN_VALUES.get(status_value.lower())
        return ("=", mapped) if mapped is not None else None

    coverage = _cached_status_value_coverage(source_id, table_fqn, column_name, [status_value], session=session)
    if coverage is False:
        return None
    return "=", status_value


def _compatible_status_filter(
    table_fqn: str, ctx: dict, status_value: str, source_id: int, *, session=None,
) -> tuple[str, str, object] | None:
    """(table_fqn, column_name, typed_value) for this ONE table, or None
    when it has no status column compatible with the requested value."""
    status_col = _find_status_column({table_fqn: ctx}, table_fqn)
    if not status_col:
        return None
    _, column_name = status_col
    col_ctx = next((c for c in ctx.get("columns", []) if c["column_name"] == column_name), None)
    if col_ctx is None:
        return None
    typed = _typed_status_filter_value(col_ctx, status_value, source_id, table_fqn, column_name, session=session)
    if typed is None:
        return None
    return table_fqn, column_name, typed[1]


def _resolve_status_compatible_entity(
    resolved_concepts: list[dict], table_contexts: dict[str, dict], status_value: str | None,
    source_id: int, *, session=None,
) -> None:
    """Bare-entity path only (mirrors the primary_table-fallback's own
    single-resolved-concept scope, immediately below this call in
    _plan_business_query_impl): when a status filter was requested and the
    concept's top-ranked selected table has no compatible status field,
    retries EXACTLY ONE further candidate from that same concept's own
    already-ranked `candidates` list — never a second retry, never a new
    candidate search. Mutates the concept entry's "selected" in place so
    every downstream reader (join planning, column selection, SQL planning)
    sees the swapped table consistently; leaves it untouched when the top
    candidate is already compatible, or when the bounded retry doesn't find
    a compatible candidate either (the existing status_column_not_found
    refusal path still applies — the filter is never silently dropped).
    """
    if not status_value:
        return
    for entry in resolved_concepts:
        selected = entry.get("selected")
        if not selected:
            continue
        top_fqn = selected["table_fqn"]
        top_ctx = table_contexts.get(top_fqn)
        if top_ctx is not None and _compatible_status_filter(top_fqn, top_ctx, status_value, source_id, session=session):
            continue  # already compatible — no retry needed

        # Exactly ONE bounded retry — the next candidate in this concept's
        # own already-ranked list, tried once and only once regardless of
        # whether it succeeds (never a scan of the whole candidate list).
        retry_candidate = next(
            (c for c in (entry.get("candidates") or []) if c.get("table_fqn") and c["table_fqn"] != top_fqn),
            None,
        )
        if retry_candidate is None:
            continue
        candidate_fqn = retry_candidate["table_fqn"]
        candidate_ctx = table_contexts.get(candidate_fqn)
        if candidate_ctx is not None and _compatible_status_filter(
            candidate_fqn, candidate_ctx, status_value, source_id, session=session,
        ):
            entry["selected"] = retry_candidate


def _live_status_value_coverage(
    source_id: int, user_id: str, table_fqn: str, column_name: str, required: list[str],
) -> bool:
    """One bounded, read-only live probe (data.investigation_service — the
    same governed, capped, never-persisted mechanism the agent's own
    investigation phase uses) — only ever reached when the cached-sample
    check above returned None (the value mapping is genuinely unknown), per
    the 'skip investigation probes unless a required value mapping is
    unknown' requirement. Fails closed (False, not a guess) on any refusal
    or error — a live-probe failure must never be treated as sufficiency."""
    from data.investigation_service import inspect_targeted_values

    result = inspect_targeted_values(
        source_id, user_id, table_fqn, column_name,
        investigation_type="distinct_values",
        reason=f"confirm '{column_name}' covers the requested status values before "
               "treating this object as sufficient",
    )
    if not result.valid or not result.sample_values:
        return False
    samples = {_normalize_value(v) for v in result.sample_values}
    return all(
        any(_normalize_value(req) == s or _normalize_value(req) in s or s in _normalize_value(req) for s in samples)
        for req in required
    )


def _check_single_object_sufficiency(
    table_fqn: str, status_values: list[str], source_id: int, user_id: str, *,
    allow_live_probe: bool, session=None,
) -> tuple[dict | None, bool]:
    """Does table_fqn ALONE already satisfy a grouped-status-count request —
    participant-level grain (a unique/PK/identity column, from profiling
    stats OR the dictionary's own is_id flag when profiling stats are
    unavailable — both already-computed metadata, not a new heuristic), a
    status-like dimension column (reusing _find_status_column's own
    evidence-ranked detection, unchanged), and value coverage (cached, or
    one live probe when allow_live_probe and the mapping is unknown)?
    Returns (result_or_None, probe_was_used) — never partially guesses."""
    ctx = get_table_business_context(source_id, user_id, table_fqn, session=session)
    if not ctx:
        return None, False
    columns = ctx.get("columns") or []
    has_grain = any(
        (c.get("schema") or {}).get("is_primary_key")
        or (c.get("schema") or {}).get("is_identity")
        or ((c.get("profiling") or {}).get("uniqueness_score") or 0.0) >= 0.95
        or (c.get("dictionary") or {}).get("is_id")
        for c in columns
    )
    if not has_grain:
        return None, False
    status_col = _find_status_column({table_fqn: ctx}, table_fqn)
    if not status_col:
        return None, False
    _, column_name = status_col

    cached = _cached_status_value_coverage(source_id, table_fqn, column_name, status_values, session=session)
    if cached is True:
        return {"table_fqn": table_fqn, "status_column": column_name}, False
    if cached is False:
        return None, False
    # cached is None -> mapping unknown
    if not allow_live_probe:
        return None, False
    if _live_status_value_coverage(source_id, user_id, table_fqn, column_name, status_values):
        return {"table_fqn": table_fqn, "status_column": column_name}, True
    return None, True


def _find_sufficient_single_object(
    entity_terms: list[str], question: str, status_values: list[str],
    source_id: int, user_id: str, *, grounding: dict | None = None, session=None,
) -> dict | None:
    """Bounded search across the entity's own candidate tables/views for one
    that alone satisfies the request. VIEWs are checked first — a curated,
    pre-built analytical object is the 'trusted' object this check prefers
    over a raw table requiring a manual join, per the source design intent.
    At most one live investigation probe total across every candidate
    checked here (not one per candidate) — the bounded 'maximum 1
    investigation probe for this query shape' this question shape gets.
    Returns None (falls through to the ordinary multi-table/join path,
    completely unchanged) when no single object is sufficient — including
    on a metadata-search infrastructure failure here: the caller's own
    retry of the same retrieval on the ordinary path (plan_business_query)
    is what actually surfaces MetadataSearchFailedError to the caller, so
    this function doesn't need its own separate reporting path for it.

    Day 2E, Task 3 — when `entity_terms` names a grounded entity, its own
    contract object(s) (preferred_view_fqn, then canonical_table_fqn) are
    checked directly and EXCLUSIVELY — never _get_ai_candidate_tables_
    ranked's broad, AI-ranked search, and never any other candidate. This
    is what makes a status-enumeration question over a grounded entity
    deterministic: previously this whole function ran independently of
    grounding, so the same question could be answered via two unrelated
    discovery mechanisms across different calls (live-confirmed to
    disagree). A grounded entity whose own object(s) don't satisfy the
    request returns None here (never falls through to the broad search
    below) — the ordinary, already contract-aware pipeline picks it up next
    with the exact same grounding.
    """
    if not status_values or not entity_terms:
        return None
    if grounding and grounding.get("grounded"):
        contract_targets: list[str] = []
        for entity_name, contract in grounding["grounded"].items():
            if not any(_term_names_grounded_entity(t.lower(), entity_name) for t in entity_terms):
                continue
            if contract.get("preferred_view_fqn"):
                contract_targets.append(contract["preferred_view_fqn"])
            if contract.get("canonical_table_fqn"):
                contract_targets.append(contract["canonical_table_fqn"])
        if contract_targets:
            probe_used = False
            for table_fqn in dict.fromkeys(contract_targets):
                result, used_probe = _check_single_object_sufficiency(
                    table_fqn, status_values, source_id, user_id,
                    allow_live_probe=not probe_used, session=session,
                )
                probe_used = probe_used or used_probe
                if result:
                    return result
            return None
    try:
        candidate_tables, ranking, _ = _get_ai_candidate_tables_ranked(
            source_id, user_id, question, entity_terms, session=session,
        )
    except MetadataSearchFailedError:
        return None
    if not candidate_tables:
        return None
    ranked_fqns = [r["qualified_name"] for r in ranking if r.get("qualified_name") in candidate_tables]
    ordered_fqns = list(dict.fromkeys(ranked_fqns + sorted(candidate_tables)))[:_SUFFICIENCY_CANDIDATE_LIMIT]

    own_connection = session is None
    conn = get_connection() if own_connection else session.conn
    try:
        placeholders = ",".join("?" * len(ordered_fqns))
        rows = conn.execute(
            f"SELECT table_fqn, table_type FROM data_dictionary_tables "
            f"WHERE source_id = ? AND table_fqn IN ({placeholders})",
            (source_id, *ordered_fqns),
        ).fetchall()
    finally:
        if own_connection:
            conn.close()
    table_types = {r["table_fqn"]: (r["table_type"] or "").upper() for r in rows}
    ordered_fqns.sort(key=lambda fqn: 0 if table_types.get(fqn) == "VIEW" else 1)

    probe_used = False
    for table_fqn in ordered_fqns[:_SUFFICIENCY_INSPECT_LIMIT]:
        result, used_probe = _check_single_object_sufficiency(
            table_fqn, status_values, source_id, user_id,
            allow_live_probe=not probe_used, session=session,
        )
        probe_used = probe_used or used_probe
        if result:
            return result
    return None


def _build_sufficient_object_plan(
    source_id: int, user_id: str, question: str, entity_terms: list[str],
    sufficient: dict, query_intent: dict, remembered_terminology: list,
) -> dict:
    """Builds a complete query_plan directly against the single sufficient
    object found above — reuses the same real assembly functions
    (_select_entity_key, _plan_joins, _build_intent, _compute_confidence,
    _build_explanation) the ordinary multi-table path uses, just skipping
    the expensive candidate-table/relationship search (and the lexical
    term-to-table scoring _resolve_entity_count would otherwise redo —
    pointless here, since _check_single_object_sufficiency already
    positively verified this exact table, not merely ranked it best guess)
    that path would otherwise run. _plan_joins itself already returns
    required=False with zero relationship-search calls whenever it's
    handed a single table — unchanged, reused as-is; see _plan_joins."""
    table_fqn = sufficient["table_fqn"]
    status_column = sufficient["status_column"]
    ctx = get_table_business_context(source_id, user_id, table_fqn)

    entity_term = entity_terms[0]
    key = _select_entity_key(ctx)
    measure_selected = {
        "table_fqn": table_fqn, "column_name": key["column_name"] if key else None,
        "business_label": None, "score": 1.0, "distinct": False,
        "key_tier": key["tier"] if key else None,
        "key_confidence": key["confidence"] if key else "none",
        "key_selection_reason": (
            f"COUNT({key['column_name']}) — {key['reason']}." if key else
            "No reliable entity key found — using COUNT(*) on the single verified-sufficient object."
        ),
        "aggregation_target": "entity_count",
    }
    measures = [{
        "term": entity_term, "selected": measure_selected,
        "candidates": [measure_selected], "warnings": [],
    }]

    status_col_ctx = next(
        (c for c in (ctx.get("columns") or []) if c["column_name"] == status_column), None,
    )
    dic = (status_col_ctx or {}).get("dictionary") or {}
    dimension_candidate = {
        "table_fqn": table_fqn, "column_name": status_column,
        "business_label": dic.get("business_label"), "score": 1.0,
    }
    dimensions = [{
        "term": "status", "selected": dict(dimension_candidate),
        "candidates": [dimension_candidate], "warnings": [], "semantic_compatibility": None,
    }]

    resolved_concepts = [{
        "term": entity_term, "resolved": True,
        "selected": {"table_fqn": table_fqn, "score": 1.0},
        "candidates": [{"table_fqn": table_fqn, "score": 1.0}],
        "confidence": 1.0, "ambiguity_reason": None,
    }]

    join_plan = _plan_joins(source_id, user_id, table_fqn, {table_fqn})
    warnings: list[dict] = []
    intent = _build_intent(question, measures, dimensions, query_intent, resolved_concepts)
    confidence = _compute_confidence(measures, dimensions, join_plan, warnings)
    explanation = _build_explanation(measures, dimensions, join_plan, warnings)

    return {
        "source_id": source_id,
        "intent": intent,
        "tables": [table_fqn],
        "columns": {table_fqn: [c["column_name"] for c in (ctx.get("columns") or [])]},
        "measures": measures,
        "dimensions": dimensions,
        "concepts": resolved_concepts,
        "filters": [],
        "join_plan": join_plan,
        "warnings": warnings,
        "confidence": confidence,
        "explanation": explanation,
        "candidate_ranking": [],
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": [],
        "entity": None, "entity_table": None,
        "entity_confidence": None, "entity_candidates": [],
        "sufficiency_shortcut": {
            "table_fqn": table_fqn, "status_column": status_column,
            "reason": "A single trusted object satisfied grain + dimension + value-coverage; "
                      "relationship/join search was skipped entirely.",
        },
    }


# ---------------------------------------------------------------------------
# Day 2D, Priority 2 — fast exit for a question naming only entities already
# confirmed unsupported (NO_SAFE_SELECTION/NO_CANDIDATE) by their own cached
# semantic contract. See plan_business_query's own fast-exit check, right
# after grounding is computed, for the trigger condition.
# ---------------------------------------------------------------------------

def _unsupported_entity_message(entity_name: str, contract: dict) -> str:
    """Day 2E, Task 5 — a business-level refusal naming ONLY the business
    concept, never a physical table. _discover_entity_contract_
    deterministic's own NO_CANDIDATE evidence text distinguishes "genuinely
    nothing found" (its very first, bounded-retrieval-stage message
    literally says 'no tables') from every other NO_CANDIDATE/NO_SAFE_
    SELECTION reason (ranking/confidence/validation all imply at least one
    real candidate was inspected and rejected) — reused here as the only
    signal for which of the two business-facing phrasings applies, rather
    than inventing a new classification. No table_fqn, business_name, or
    any other physical identifier ever appears in either phrasing.
    """
    evidence = ((contract.get("contract") or {}).get("evidence")) or []
    nothing_found = bool(evidence) and "no tables" in evidence[0].lower()
    if nothing_found:
        return f"I could not identify a reliable {entity_name} data source in the connected database."
    return f"I found several {entity_name.lower()}-related datasets, but none is verified as the canonical source."


def _build_unsupported_entity_plan(
    source_id: int, question: str, grounding: dict, query_intent: dict, remembered_terminology: list,
) -> dict:
    """Builds a complete, immediately-blocking query_plan directly from
    grounding["unsupported"] — contracts already fetched (cache hit or a
    single bounded adjudication attempt, per get_or_build_entity_contract's
    own metadata_revision freshness check) by apply_grounding's own loop
    above. No new DB/AI call here, and — critically — the caller never runs
    _get_ai_candidate_tables_ranked, candidate hydration, or join planning
    for this question: there is nothing left to search for an entity a
    verified contract already proved has no reliable canonical table for
    the CURRENT metadata revision. A future revalidate (triggered by a
    metadata_revision change, not by re-asking the same question) is the
    only thing that can change this — see get_or_build_entity_contract.

    Day 2E, Task 5 — `business_messages` carries one plain-English,
    table-free sentence per unsupported entity (_unsupported_entity_
    message) — the intended FINAL answer text for this plan, read by
    core.orchestrator.agent.answer_business_question to short-circuit
    straight to a SAFELY_REFUSED business answer, bypassing build_sql_plan/
    generate_sql and the generic "Generation refused: ..." explanation
    pipeline entirely for this case (which would otherwise list the raw,
    technical unresolved-term reasons instead of a business-worded one).
    `warnings` keeps the technical detail (entity name + original
    resolution_status + raw evidence) for developer diagnostics only.
    """
    warnings: list[dict] = []
    business_messages: list[str] = []
    for entity_name, contract in grounding["unsupported"].items():
        evidence = ((contract.get("contract") or {}).get("evidence")) or []
        business_messages.append(_unsupported_entity_message(entity_name, contract))
        warnings.append({
            "type": "unsupported_entity", "severity": "HIGH",
            "message": (
                f"'{entity_name}' has no verified data model in this source "
                f"(status={contract.get('resolution_status')})."
                + (f" {evidence[0]}" if evidence else "")
            ),
        })

    intent = _build_intent(question, [], [], query_intent, [])
    return {
        "source_id": source_id,
        "intent": intent,
        "tables": [],
        "columns": {},
        "measures": [],
        "dimensions": [],
        "concepts": [],
        "filters": [],
        "join_plan": {
            "required": False, "tables": [], "primary_table": None,
            "steps": [], "fanout_risk": None, "confidence": 100,
        },
        "warnings": warnings,
        "confidence": 0,
        "explanation": [w["message"] for w in warnings],
        "candidate_ranking": [],
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": [],
        "entity": None, "entity_table": None,
        "entity_confidence": None, "entity_candidates": [],
        "unsupported_entities": sorted(grounding["unsupported"].keys()),
        "business_messages": business_messages,
    }


# ---------------------------------------------------------------------------
# Step 6 — Join planning (composes analyze_join_quality / recommend_best_join_path)
# ---------------------------------------------------------------------------

def _plan_one_join(source_id: int, user_id: str, table_a: str, table_b: str) -> list[dict]:
    """
    Returns a list of granular, single-hop join steps (never a multi-hop
    aggregate) — one dict per relationship edge actually traversed, each
    carrying the real from_column/to_column. A direct join is a 1-element
    list; a multi-hop path is expanded to one element per edge, sourced from
    recommend_best_join_path's best_join_path["edges"] (Phase 2 already
    computes full per-edge detail there — previously discarded down to just
    aggregate stats, which left no column names for Phase 4's JOIN planning
    to use).
    """
    direct = analyze_join_quality(source_id, user_id, table_a, table_b)
    if direct and direct.get("best_join"):
        b = direct["best_join"]
        return [{
            "from_table": b["from_table_fqn"], "from_column": b["from_column"],
            "to_table":   b["to_table_fqn"],   "to_column":   b["to_column"],
            "path_found": True, "hops": 1,
            "join_type": b["join_type"], "cardinality": b["cardinality"],
            "fanout_risk": b["fanout_risk"], "fanout_explanation": b["fanout_explanation"],
            "join_quality": b["join_quality"], "join_quality_tier": b["join_quality_tier"],
            "relationship_strength": b["relationship_strength"],
            "confidence": b["relationship_confidence"],
        }]

    indirect = recommend_best_join_path(source_id, user_id, table_a, table_b)
    best_path = indirect.get("best_join_path") if indirect else None
    if best_path and best_path["hops"] > 0:
        return [
            {
                "from_table": edge["from_table_fqn"], "from_column": edge["from_column"],
                "to_table":   edge["to_table_fqn"],   "to_column":   edge["to_column"],
                "path_found": True, "hops": 1,
                "join_type": edge["join_type"], "cardinality": edge["cardinality"],
                "fanout_risk": edge["fanout_risk"], "fanout_explanation": edge["fanout_explanation"],
                "join_quality": edge["join_quality"], "join_quality_tier": edge["join_quality_tier"],
                "relationship_strength": edge["relationship_strength"],
                "confidence": edge["relationship_confidence"],
            }
            for edge in best_path["edges"]
        ]

    return [{
        "from_table": table_a, "from_column": None,
        "to_table":   table_b, "to_column":   None,
        "path_found": False, "hops": None,
        "join_type": None, "cardinality": None,
        "fanout_risk": None, "fanout_explanation": None,
        "join_quality": None, "join_quality_tier": None,
        "relationship_strength": None, "confidence": 0,
    }]


def _plan_joins(source_id: int, user_id: str, primary_table: str | None, selected_tables: set[str]) -> dict:
    """
    Step 6. 0/1 table -> no join needed. 2+ tables -> a star-join plan around
    primary_table (the table holding the primary measure, or a deterministic
    fallback anchor), built entirely from pairwise calls to the existing
    Phase 2 join-quality functions — no new multi-table join optimizer.
    """
    if primary_table is None and selected_tables:
        primary_table = sorted(selected_tables)[0]

    other_tables = sorted(selected_tables - ({primary_table} if primary_table else set()))

    if not other_tables:
        tables = [primary_table] if primary_table else []
        return {
            "required": False, "tables": tables, "primary_table": primary_table,
            "steps": [], "fanout_risk": None, "confidence": 100,
        }

    steps: list[dict] = []
    for t in other_tables:
        steps.extend(_plan_one_join(source_id, user_id, primary_table, t))

    fanout_levels = {s["fanout_risk"] for s in steps if s.get("fanout_risk")}
    worst_fanout = next((lvl for lvl in ("HIGH", "MEDIUM", "LOW") if lvl in fanout_levels), None)

    confidences = [s["confidence"] for s in steps if s.get("path_found")]
    avg_conf = round(sum(confidences) / len(confidences)) if confidences else 0

    return {
        "required": True,
        "tables": [primary_table] + other_tables,
        "primary_table": primary_table,
        "steps": steps,
        "fanout_risk": worst_fanout,
        "confidence": avg_conf,
    }


# ---------------------------------------------------------------------------
# Step 7 — Governance / trust warnings (derived from data already gathered)
# ---------------------------------------------------------------------------

def _collect_governance_warnings(
    table_contexts: dict, measures: list[dict], dimensions: list[dict], join_plan: dict,
) -> list[dict]:
    warnings: list[dict] = []

    for entry in measures + dimensions:
        sel = entry.get("selected")
        if not sel:
            continue
        ctx = table_contexts.get(sel["table_fqn"])
        if not ctx:
            continue
        col = next((c for c in ctx["columns"] if c["column_name"] == sel["column_name"]), None)
        if col is None:
            continue

        dic = col.get("dictionary")
        if not dic or not dic.get("is_approved"):
            warnings.append({
                "type": "metadata_not_approved", "severity": "LOW",
                "message": f"{sel['table_fqn']}.{sel['column_name']} has no approved dictionary entry.",
            })

        prof = col.get("profiling")
        pii_flagged = (prof and prof.get("pii_name_heuristic")) or (dic and dic.get("pii_risk"))
        if pii_flagged:
            confirmed = bool(prof and prof.get("pii_confirmed"))
            warnings.append({
                "type": "pii_involved", "severity": "MEDIUM" if confirmed else "HIGH",
                "message": (
                    f"{sel['table_fqn']}.{sel['column_name']} may contain PII "
                    f"({'confirmed' if confirmed else 'unconfirmed'})."
                ),
            })

    for step in join_plan.get("steps", []):
        if not step.get("path_found"):
            warnings.append({
                "type": "no_join_path_found", "severity": "HIGH",
                "message": f"No trusted join path found between {step['from_table']} and {step['to_table']}.",
            })
            continue
        if step.get("fanout_risk") in ("MEDIUM", "HIGH"):
            warnings.append({
                "type": "high_fanout_risk" if step["fanout_risk"] == "HIGH" else "fanout_risk",
                "severity": "HIGH" if step["fanout_risk"] == "HIGH" else "MEDIUM",
                "message": step.get("fanout_explanation") or (
                    f"Join between {step['from_table']} and {step['to_table']} has "
                    f"{step['fanout_risk']} fan-out risk."
                ),
            })
        if step.get("relationship_strength") == "WEAK" or step.get("join_quality_tier") in ("LOW", "MEDIUM"):
            warnings.append({
                "type": "low_confidence_relationship", "severity": "MEDIUM",
                "message": f"Join between {step['from_table']} and {step['to_table']} has low confidence.",
            })

    return warnings


# ---------------------------------------------------------------------------
# Intent / confidence / explanation
# ---------------------------------------------------------------------------

def _infer_aggregation(selected_measure: dict | None, question_aggregation: str | None) -> str | None:
    """
    question_aggregation (from core.semantic.concept_resolver.extract_query_intent,
    detected straight from the question text — COUNT/SUM/AVG/MIN/MAX) is the
    primary signal now: it reflects what the user actually asked for, not just
    the selected column's own name. Falls back to the original column-name
    heuristic, and finally to "SUM", exactly preserving prior behavior for
    questions with no explicit aggregation language (e.g. "revenue by status").
    """
    if question_aggregation:
        return question_aggregation
    if not selected_measure:
        return None
    name = f"{selected_measure.get('column_name') or ''} {selected_measure.get('business_label') or ''}"
    toks = set(_tokenize(name))
    for tok, agg in _AGGREGATION_HINTS.items():
        if tok in toks:
            return agg
    return "SUM"


# ---------------------------------------------------------------------------
# Day 2A, Task 3 — Entity-First List Routing.
#
# Confirmed defect (verified against real CCPP metadata): "List open job
# orders" resolves its concept correctly to dbo.ADF_BHJobs, but the same
# term ALSO happens to name-match an unrelated pre-aggregated rollup column
# (dbo.ADF_BHClientContacts.TotalJobOrders) via the ordinary measure-column
# search — extract_terms() mirrors a bare (no "by"-clause) term into both
# `concepts` and `measures`, so any bare list/show/display/return question
# always tries a measure-column match too. When that incidental match wins,
# has_measure=True unconditionally forced qtype to "aggregate_overall" and
# _infer_aggregation defaulted to SUM, silently overriding an explicit list
# request that used no aggregation language at all.
#
# A measure is now trusted to override list/entity routing only when:
#   (a) the question itself uses explicit aggregation language (query_intent
#       ["aggregation"] is not None — extract_query_intent's own COUNT/SUM/
#       AVG/MIN/MAX detection: "how many", "total", "average", "sum of",
#       "highest", ...), or
#   (b) the measure's own table is the SAME table a resolved concept/entity
#       already resolved to — i.e. it's a genuine attribute of the entity
#       being listed, not a coincidental match on a different table.
# Any other selected measure is treated as incidental for SHAPE purposes
# only: it still appears in the plan's own "measures" list unchanged (full
# explainability preserved), it just doesn't get to decide has_measure/
# primary_measure for qtype/aggregation.
# ---------------------------------------------------------------------------

def _has_explicit_aggregation_language(query_intent: dict) -> bool:
    return query_intent.get("aggregation") is not None


def _is_incidental_measure_match(selected_measure: dict, resolved_concepts: list[dict] | None) -> bool:
    concept_tables = {
        c["selected"]["table_fqn"] for c in (resolved_concepts or []) if c.get("selected")
    }
    if not concept_tables:
        return False  # no resolved entity to compare against — never override on this signal alone
    return selected_measure.get("table_fqn") not in concept_tables


def _entity_first_effective_measures(
    measures: list[dict], query_intent: dict, resolved_concepts: list[dict] | None,
) -> list[dict]:
    if _has_explicit_aggregation_language(query_intent):
        return measures
    return [
        {**m, "selected": None}
        if m.get("selected") and _is_incidental_measure_match(m["selected"], resolved_concepts)
        else m
        for m in measures
    ]


def _build_intent(
    question: str, measures: list[dict], dimensions: list[dict], query_intent: dict,
    resolved_concepts: list[dict] | None = None,
) -> dict:
    measures = _entity_first_effective_measures(measures, query_intent, resolved_concepts)
    has_measure = any(m["selected"] for m in measures)
    has_dimension = any(d["selected"] for d in dimensions)
    if has_measure and has_dimension:
        qtype = "aggregate_by_dimension"
    elif has_measure:
        qtype = "aggregate_overall"
    elif has_dimension:
        # joined_detail_list — "names of the students" carries no measure
        # and its one real content concept ("students") resolves confidently
        # while its only dimension ("names") resolved on a JOINED table via
        # _resolve_related_attribute_dimension. extract_terms() puts the
        # attribute word into `concepts` too (mirroring the "by"-clause
        # contract), so attribute-keyword entries are excluded before
        # counting — the same single-real-concept discipline list_entities
        # below already applies, just attribute-aware.
        non_attribute_concepts = [
            c for c in (resolved_concepts or [])
            if (c.get("term") or "").lower() not in _NAME_ATTRIBUTE_TERMS
        ]
        resolved = [c for c in non_attribute_concepts if c.get("selected")]
        qtype = (
            "joined_detail_list"
            if len(non_attribute_concepts) == 1 and len(resolved) == 1
            else "list_by_dimension"
        )
    else:
        # Enterprise AI Analyst Agent — bare entity list routing. A question
        # naming only a business concept ("Show clients", "List invoices")
        # with no measure/dimension term previously fell through to
        # "unresolved" even when the concept resolved confidently to a
        # single table, because sql_planning_service never built SELECT from
        # concepts — it silently refused instead of listing the entity. Only
        # routes to list_entities when exactly one concept term was
        # extracted and it resolved: a multi-concept bare request ("show
        # clients and invoices") stays "unresolved" rather than guessing
        # which one (or inventing a join/union) — the same never-guess-on-
        # genuine-ambiguity rule used everywhere else in this module.
        resolved = [c for c in (resolved_concepts or []) if c.get("selected")]
        qtype = "list_entities" if len(resolved_concepts or []) == 1 and len(resolved) == 1 else "unresolved"
    primary_measure = next((m["selected"] for m in measures if m["selected"]), None)
    return {
        "raw_question": question,
        "type":         qtype,
        "aggregation":  _infer_aggregation(primary_measure, query_intent.get("aggregation")),
        # Milestone Phase 6.2 — explicitly recorded on the plan itself,
        # straight from extract_query_intent's own classification (never
        # re-derived here) so the aggregation-shape decision is traceable
        # end to end from question text through to sql_plan["aggregation_plan"].
        "aggregation_target": query_intent.get("aggregation_target"),
        "distinct":     query_intent.get("distinct", False),
        "order":        query_intent.get("order"),
    }


# ---------------------------------------------------------------------------
# Day 3 — Multi-Entity Relationship Questions. A NEW, additive routing
# branch tried by _plan_business_query_impl BEFORE the legacy single-entity
# selected_tables/_build_intent path below it in that function. _build_intent
# itself and the selected_tables construction that feeds it are completely
# untouched by this branch — it only ever short-circuits with its own
# return, never mutates or narrows anything the legacy path reads. No second
# join planner or SQL generator: _plan_joins immediately below is the exact
# function the single-entity path already calls, and the returned query_plan
# dict is shaped identically to _plan_business_query_impl's own return so
# sql_planning_service.build_sql_plan / sql_generation_service /
# query_execution_service.execute_governed_query all consume it unchanged.
# ---------------------------------------------------------------------------

_MAX_RELATIONSHIP_ENTITIES = 3


def _relationship_nearest_entity_distance(question: str, entity_name: str, target_pos: int) -> int | None:
    """Character distance from `target_pos` to the nearest occurrence of any
    of `entity_name`'s own taxonomy synonyms in `question` (case-insensitive),
    or None if the entity isn't textually mentioned at all. Used only to
    disambiguate WHICH grounded entity a filter word belongs to when more
    than one entity's contract could otherwise satisfy it — never used to
    decide whether a term names an entity in the first place (that's still
    semantic_contract_service.match_entities_for_terms, unchanged)."""
    from data.semantic_contract_service import ENTITY_TAXONOMY
    lower_question = question.lower()
    best: int | None = None
    for synonym in ENTITY_TAXONOMY.get(entity_name, ()):
        idx = lower_question.find(synonym)
        while idx != -1:
            dist = abs(idx - target_pos)
            if best is None or dist < best:
                best = dist
            idx = lower_question.find(synonym, idx + 1)
    return best


def _resolve_relationship_status_filter(
    question: str, status_value: str, relationship_entities: list[str], grounding: dict, allowed_tables: set[str],
) -> tuple[str, str, object] | None:
    """
    Day 3, Task 3 — binds a requested status word ("open", "active", ...) to
    the ONE relationship entity whose semantic contract has a VERIFIED status
    column carrying that exact value (grounding["status_hints"] — the same
    contract-verified source the legacy single-entity path already prefers
    over its own generic keyword-based _find_status_column fallback, Day 2B
    Task 4). Never falls back to the generic finder here: an unverified guess
    is exactly the failure mode Task 3 forbids ("do not apply 'open' to
    Client when it describes Job Order") — real CCPP checked live confirms
    the generic fallback would otherwise attach an unrelated column/value
    pair with no evidence it means what the user said.

    When more than one relationship entity's contract verifies the same
    value (a real possibility — two entities can legitimately share a status
    vocabulary), the filter is bound to whichever entity's own name is
    textually CLOSEST to the status word in the question ("open job orders"
    binds to Job Order, not Client, purely from word proximity — no
    positional assumption baked in beyond "nearest mention wins"). A genuine
    tie, or zero entities with a verified match, returns None — the caller
    then leaves the whole relationship route un-fired rather than guess.
    """
    normalized = status_value.lower()
    lower_question = question.lower()
    status_pos = lower_question.find(normalized)
    status_hints = grounding.get("status_hints") or {}
    entity_table_map = grounding.get("entity_table_map") or {}
    candidates: list[tuple[int, str, str, str, object]] = []
    for entity_name in relationship_entities:
        table_fqn = entity_table_map.get(entity_name)
        if table_fqn is None or table_fqn not in allowed_tables:
            continue
        hint = status_hints.get(table_fqn)
        if not hint:
            continue
        if hint.get("data_type") == "BOOLEAN" and normalized in _STATUS_BOOLEAN_VALUES:
            typed_value = _STATUS_BOOLEAN_VALUES[normalized]
        else:
            matched_value = next(
                (v for v in (hint.get("verified_values") or []) if v.lower() == normalized), None,
            )
            if matched_value is None:
                continue
            typed_value = matched_value
        distance = _relationship_nearest_entity_distance(question, entity_name, status_pos)
        candidates.append((
            distance if distance is not None else 10**9, entity_name, table_fqn, hint["column_name"], typed_value,
        ))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None
    _, _, table_fqn, column_name, typed_value = candidates[0]
    return table_fqn, column_name, typed_value


def _try_build_relationship_plan(
    source_id: int, user_id: str, question: str, grounding: dict | None, query_intent: dict,
    table_contexts: dict, candidate_ranking: list[dict], remembered_terminology: list[dict],
    generated_vocabulary_evidence: list[dict], warnings: list[dict],
    resolved_concepts: list[dict] | None = None,
) -> dict | None:
    """
    Fires only for a bare "show <entity> and their <entity>[, and <entity>]"
    style relationship question, optionally with a status filter naming one
    of the grounded entities (Day 3, Task 3): 2-3 business entities all fully
    grounded via semantic_contract_service.apply_grounding, with no
    aggregation/date/order/null-check term also requested — those question
    shapes are handled by _try_build_relationship_aggregate_plan or stay on
    the legacy path (this gate deliberately stays narrow so it can only ever
    ADD capability, never change an existing answer). Returns None whenever
    the gate doesn't fully match, so the caller's existing single-entity
    selected_tables/_build_intent path takes over exactly as it does today —
    including the case where an unmatched or partially-grounded entity means
    fully_grounded is False.

    Join safety is inherited, not reimplemented: _plan_joins only ever uses
    trusted (AUTO/APPROVED) relationship edges, and build_sql_plan's own
    existing untrusted-join check refuses the plan if any requested entity
    can't be reached — so an entity with no trusted path to the others is
    surfaced as a normal refusal, never a silent drop or a guessed join.
    """
    if not grounding or not grounding.get("fully_grounded"):
        return None

    relationship_entities = grounding["matched_entities"]
    if not (2 <= len(relationship_entities) <= _MAX_RELATIONSHIP_ENTITIES):
        return None

    if (
        query_intent.get("aggregation") is not None
        or query_intent.get("aggregation_target") is not None
        or query_intent.get("date_range")
        or query_intent.get("order")
        or query_intent.get("null_check_requested")
    ):
        return None

    entity_table_map = grounding["entity_table_map"]
    if not all(e in entity_table_map for e in relationship_entities):
        # A caller-supplied grounding dict can legitimately claim
        # fully_grounded=True (every matched entity has a "grounded" entry)
        # without entity_table_map carrying every one of those entities —
        # real apply_grounding() output never does this, but this function
        # must not assume its own caller's contract that strictly (verified
        # live by a unit test constructing exactly this shape). Fall through
        # to the legacy path rather than KeyError.
        return None
    selected_tables = {entity_table_map[e] for e in relationship_entities}
    if len(selected_tables) != len(relationship_entities):
        # Two "different" grounded entities resolving to the SAME physical
        # table is not a real relationship — it's one physical object a
        # compound business phrase names twice (e.g. a table literally
        # called "student_enrollments" independently satisfying both the
        # Student and Enrollment taxonomy entries). That's exactly what the
        # existing compound-phrase resolution (generate_compound_phrase_
        # candidates / _resolve_role_with_compound_preference) already
        # handles as ONE concept — verified live by a regression this branch
        # broke before this check existed. Fall through to the legacy path.
        return None
    if not selected_tables.issubset(table_contexts.keys()):
        # Grounding named a table this call's candidate/table_contexts set
        # never seeded (should not happen — apply_grounding always seeds a
        # grounded entity's own canonical table — defensive only). Fall
        # through to the legacy path rather than build a plan referencing a
        # table with no known columns.
        return None

    # Day 3, Task 3 — a status filter must bind to exactly one relationship
    # entity's own VERIFIED contract value; _resolve_relationship_status_
    # filter returns None on no-match or a genuine tie, and this route
    # refuses (falls through to the legacy path) rather than either drop the
    # filter silently or attach it to the wrong entity.
    resolved_filters: list[dict] = []
    if query_intent.get("status_value"):
        status_col = _resolve_relationship_status_filter(
            question, query_intent["status_value"], relationship_entities, grounding, selected_tables,
        )
        if status_col is None:
            return None
        fqn, col_name, typed_value = status_col
        resolved_filters.append({
            "column": col_name, "operator": "=", "value": typed_value, "resolved": True, "table_fqn": fqn,
        })

    # Live-verified against real CCPP — anchoring on whichever entity was
    # simply mentioned first can leave _plan_joins with no direct edge to a
    # third entity, falling back to a long, sometimes semantically bogus
    # indirect path (confirmed: anchoring "students, their enrollments, and
    # course details" on Student routed Course's join through an unrelated
    # Users/video-presenter chain instead of through Enrollment, already in
    # the join set, which has a much shorter real path to Course). _plan_joins
    # itself is untouched and still the only join planner in play — this
    # just tries it once per candidate anchor (bounded by
    # _MAX_RELATIONSHIP_ENTITIES, so at most 3 calls) and keeps whichever
    # anchor yields the fewest total join steps, i.e. the least speculative
    # combined path across every requested entity. Ties keep the
    # first-mentioned entity, preserving today's behavior whenever every
    # anchor is equally good (e.g. the common 2-entity, direct-edge case).
    primary_table = None
    join_plan = None
    for candidate_entity in relationship_entities:
        candidate_primary = entity_table_map[candidate_entity]
        candidate_plan = _plan_joins(source_id, user_id, candidate_primary, selected_tables)
        if join_plan is None or len(candidate_plan["steps"]) < len(join_plan["steps"]):
            primary_table, join_plan = candidate_primary, candidate_plan

    # Prefer the caller's own already-resolved concept entry for this entity
    # (real user-typed term, e.g. "clients" — _resolve_concept independently
    # confirms the same grounded table via grounding's own narrowing) so a
    # consumer keying off query_plan["concepts"] by the original term (e.g.
    # citations, the entity-independently-resolved test contract) still
    # finds it. entity_table_map's canonical table is always the one that
    # actually gets used for SELECT/joins either way — grounded is grounded
    # — this only affects which term label/candidates are attached; an
    # entity with no independently-resolved concept entry (e.g. its name was
    # only recognized via a multi-word synonym split) still gets a synthetic
    # entry keyed by its taxonomy name rather than being silently dropped.
    concepts = []
    for entity_name in relationship_entities:
        table_fqn = entity_table_map[entity_name]
        match = next(
            (c for c in (resolved_concepts or [])
             if c.get("selected") and c["selected"].get("table_fqn") == table_fqn),
            None,
        )
        if match:
            concepts.append(match)
        else:
            concepts.append({
                "term": entity_name,
                "confidence": 1.0,
                "candidates": [{"table_fqn": table_fqn, "score": 100.0, "source": "semantic_contract"}],
                "selected": {"table_fqn": table_fqn, "score": 100.0, "source": "semantic_contract"},
                "warnings": [],
            })

    intent = {
        "raw_question": question,
        "type": "relationship_list",
        "aggregation": None,
        "aggregation_target": None,
        "distinct": query_intent.get("distinct", False),
        "order": None,
        "relationship_entities": relationship_entities,
    }
    explanation = _build_explanation([], [], join_plan, warnings)
    explanation += (
        f" Relationship question across {len(relationship_entities)} entities: "
        f"{', '.join(relationship_entities)}."
    )

    # Day 3, Task 5 — list fanout safety. A one-to-many/many-to-many join
    # legitimately produces more than one row per primary entity (e.g. one
    # Student row per Enrollment) — that's correct data, not a bug, so this
    # never adds DISTINCT (which would silently discard real rows). It's
    # purely informational: make the shape explicit rather than let a
    # reader assume one row per primary entity when it isn't so.
    if join_plan.get("fanout_risk") in ("MEDIUM", "HIGH"):
        explanation += (
            " Note: this join can produce more than one row for the same "
            f"{relationship_entities[0]} — rows are not deduplicated, since "
            "doing so would remove legitimate records."
        )
        warnings.append({
            "type": "relationship_list_fanout", "severity": "LOW",
            "message": (
                f"This relationship join carries {join_plan['fanout_risk']} fan-out risk — "
                "expect more than one row per primary entity where the relationship is one-to-many."
            ),
        })

    return {
        "source_id":   source_id,
        "intent":      intent,
        "tables":      sorted(table_contexts.keys()),
        "columns":     {fqn: [c["column_name"] for c in ctx["columns"]] for fqn, ctx in table_contexts.items()},
        "measures":    [],
        "dimensions":  [],
        "concepts":    concepts,
        "filters":     resolved_filters,
        "join_plan":   join_plan,
        "warnings":    warnings,
        "confidence":  _compute_confidence([], [], join_plan, warnings),
        "explanation": explanation,
        "candidate_ranking": candidate_ranking,
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": generated_vocabulary_evidence,
        "entity": None,
        "entity_table": None,
        "entity_confidence": None,
        "entity_candidates": [],
        "metadata_search_failed": False,
        "metadata_search_failed_reason": None,
        "grounding": {
            "matched_entities": grounding["matched_entities"],
            "grounded_entities": list(grounding["grounded"].keys()),
            "fully_grounded": grounding["fully_grounded"],
            "seed_table_fqns": sorted(grounding["seed_table_fqns"]),
        },
    }


_GROUP_LABEL_SEMANTIC_PRIORITY = ("NAME", "ID", "CODE", "STATUS", "DATE")


def _pick_relationship_group_column(entry: dict) -> dict | None:
    """
    Day 3, Task 4 — when _resolve_term's own ambiguity gate left
    entry["selected"] unset among several tied candidates, pick the best
    GROUP BY label deterministically instead of refusing outright. This is
    safe specifically because the caller only ever passes a SINGLE-TABLE-
    scoped table_contexts here (one already-grounded entity's own table) —
    every candidate already belongs to the correct entity, so this is a
    preference among one entity's own columns, never a guess about which
    entity or table. Prefers a NAME-semantic column first (what a business
    user would recognize the entity by — e.g. CourseName over CourseDesc/
    CourseActive), then an identifier/code, then status/date, then whatever
    ranked highest — the same deterministic column-preference order
    data.sql_planning_service._select_safe_list_entity_columns already uses
    for the analogous bare-entity-list case, applied here to pick one column
    instead of many.
    """
    if entry.get("selected"):
        return entry["selected"]
    candidates = entry.get("candidates") or []
    if not candidates:
        return None
    for semantic_type in _GROUP_LABEL_SEMANTIC_PRIORITY:
        match = next((c for c in candidates if c.get("semantic_type") == semantic_type), None)
        if match:
            return match
    return candidates[0]


def _try_build_relationship_aggregate_plan(
    source_id: int, user_id: str, question: str, grounding: dict | None, query_intent: dict,
    table_contexts: dict, measure_terms: list[str], candidate_ranking: list[dict],
    remembered_terminology: list[dict], generated_vocabulary_evidence: list[dict], warnings: list[dict],
    concept_mappings: dict | None, generated_vocab: dict | None,
) -> dict | None:
    """
    Day 3, Task 4 — relationship counts/aggregates: "How many students are
    enrolled in each course?", "Total invoice amount by client." Fires only
    for exactly 2 fully-grounded relationship entities where the question's
    aggregation targets ONE of them (entity_count/distinct_entity_count, or a
    stored metric column via measure_sum/average/min/max) and the OTHER
    becomes the GROUP BY dimension. WHICH entity plays which role is decided
    by semantic_contract_service.entity_consumed_terms — the same "does this
    term belong to this entity's own name" check Day 2E already uses — never
    a positional guess. Reuses the exact same _resolve_entity_count/
    _resolve_term the legacy single-entity path uses for the measure itself,
    each scoped to only the relevant entity's own table: no second
    aggregation engine, just deciding which grounded entity supplies which
    role before handing off to the same resolvers.

    Grain safety is inherited from _apply_join_fanout_safety (unchanged,
    called below exactly as the legacy path calls it) — an entity-count
    across a MEDIUM/HIGH fan-out join is only kept when a trusted key can
    COUNT(DISTINCT ...), and refused outright otherwise, never silently
    inflated.
    """
    aggregation_target = query_intent.get("aggregation_target")
    if aggregation_target not in (
        "entity_count", "distinct_entity_count",
        "measure_sum", "measure_average", "measure_min", "measure_max",
    ):
        return None
    if not grounding or not grounding.get("fully_grounded"):
        return None
    relationship_entities = grounding["matched_entities"]
    if len(relationship_entities) != 2:
        return None
    if query_intent.get("date_range") or query_intent.get("order") or query_intent.get("status_value"):
        return None

    entity_table_map = grounding["entity_table_map"]
    entity_tables = {entity_table_map.get(e) for e in relationship_entities}
    if not entity_tables.issubset(table_contexts.keys()):
        return None
    if len(entity_tables) != len(relationship_entities):
        # Two "different" grounded entities resolving to the SAME physical
        # table is not a real relationship — see the identical check/comment
        # in _try_build_relationship_plan for the live-verified regression
        # this guards against.
        return None

    from data.semantic_contract_service import ENTITY_TAXONOMY, entity_consumed_terms

    measure_entity = None
    for e in relationship_entities:
        if entity_consumed_terms(e, measure_terms):
            if measure_entity is not None:
                return None
            measure_entity = e
    if measure_entity is None:
        return None
    group_entity = next(e for e in relationship_entities if e != measure_entity)

    measure_table = entity_table_map[measure_entity]
    group_table = entity_table_map[group_entity]

    if aggregation_target in ("entity_count", "distinct_entity_count"):
        count_term = ENTITY_TAXONOMY[measure_entity][0]
        measure_entry = _resolve_entity_count(
            count_term, table_contexts, distinct_requested=(aggregation_target == "distinct_entity_count"),
            concept_mappings=concept_mappings, generated_vocab=generated_vocab, grounding=grounding,
        )
    else:
        consumed = entity_consumed_terms(measure_entity, measure_terms)
        metric_terms = [t for t in measure_terms if t not in consumed]
        scoped_contexts = {measure_table: table_contexts[measure_table]}
        measure_entry = None
        for term in metric_terms:
            entry = _resolve_term(term, scoped_contexts, "measure", concept_mappings, generated_vocab, grounding=grounding)
            if entry.get("selected"):
                measure_entry = entry
                break
        if measure_entry is None:
            return None

    if not measure_entry.get("selected") or measure_entry["selected"]["table_fqn"] != measure_table:
        # Either genuinely unresolved (legacy path's own ambiguous/missing-
        # term handling is the right fallback) or — defensively — resolved
        # outside the entity it was scoped to, which should not happen given
        # the scoping above but is never trusted blindly.
        return None

    group_consumed = entity_consumed_terms(group_entity, measure_terms)
    group_term = next(iter(group_consumed), None) or ENTITY_TAXONOMY[group_entity][0]
    scoped_group_contexts = {group_table: table_contexts[group_table]}
    group_entry = _resolve_term(
        group_term, scoped_group_contexts, "dimension", concept_mappings, generated_vocab, grounding=grounding,
    )
    group_selected = _pick_relationship_group_column(group_entry)
    if group_selected is None:
        return None
    group_entry = {**group_entry, "selected": group_selected}

    selected_tables = {measure_table, group_table}
    primary_table, join_plan = None, None
    for candidate_entity in relationship_entities:
        candidate_primary = entity_table_map[candidate_entity]
        candidate_plan = _plan_joins(source_id, user_id, candidate_primary, selected_tables)
        if join_plan is None or len(candidate_plan["steps"]) < len(join_plan["steps"]):
            primary_table, join_plan = candidate_primary, candidate_plan

    measures = [measure_entry]
    dimensions = [group_entry]
    _apply_join_fanout_safety(measures, join_plan, warnings)
    if not measures[0].get("selected"):
        # Fan-out safety refused an unsafe count — a normal refusal via the
        # empty-select path below, not a relationship-specific one.
        return None

    intent = {
        "raw_question": question,
        "type": "aggregate_by_dimension",
        "aggregation": _infer_aggregation(measures[0]["selected"], query_intent.get("aggregation")),
        "aggregation_target": aggregation_target,
        "distinct": query_intent.get("distinct", False),
        "order": None,
        "relationship_entities": relationship_entities,
    }
    explanation = _build_explanation(measures, dimensions, join_plan, warnings)
    explanation += (
        f" Relationship aggregate across {len(relationship_entities)} entities: "
        f"{measure_entity} measured, grouped by {group_entity}."
    )

    concepts = [
        {
            "term": e, "confidence": 1.0,
            "candidates": [{"table_fqn": entity_table_map[e], "score": 100.0, "source": "semantic_contract"}],
            "selected": {"table_fqn": entity_table_map[e], "score": 100.0, "source": "semantic_contract"},
            "warnings": [],
        }
        for e in relationship_entities
    ]

    return {
        "source_id":   source_id,
        "intent":      intent,
        "tables":      sorted(table_contexts.keys()),
        "columns":     {fqn: [c["column_name"] for c in ctx["columns"]] for fqn, ctx in table_contexts.items()},
        "measures":    measures,
        "dimensions":  dimensions,
        "concepts":    concepts,
        "filters":     [],
        "join_plan":   join_plan,
        "warnings":    warnings,
        "confidence":  _compute_confidence(measures, dimensions, join_plan, warnings),
        "explanation": explanation,
        "candidate_ranking": candidate_ranking,
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": generated_vocabulary_evidence,
        "entity": None,
        "entity_table": None,
        "entity_confidence": None,
        "entity_candidates": [],
        "metadata_search_failed": False,
        "metadata_search_failed_reason": None,
        "grounding": {
            "matched_entities": grounding["matched_entities"],
            "grounded_entities": list(grounding["grounded"].keys()),
            "fully_grounded": grounding["fully_grounded"],
            "seed_table_fqns": sorted(grounding["seed_table_fqns"]),
        },
    }


def _try_build_partial_grounding_refusal(
    source_id: int, question: str, grounding: dict | None, query_intent: dict, remembered_terminology: list,
) -> dict | None:
    """
    Day 3, Task 6 — targeted business refusal. Fires only when this question
    names 2-3 business entities and AT LEAST ONE is grounded while AT LEAST
    ONE OTHER is a CONFIRMED-unsupported contract (grounding["unsupported"]
    — NO_CANDIDATE/NO_SAFE_SELECTION, the same dead-end classification
    _build_unsupported_entity_plan already uses for the all-unsupported
    case, Day 2D/2E). Preserves the grounded entity/entities instead of
    discarding the whole question, and explains only the missing business
    concept — reuses _unsupported_entity_message verbatim, which is
    guaranteed table-free (no table_fqn or other physical identifier ever
    appears in its wording), so this never surfaces a physical table picker
    to the user. Physical detail (which contract, which resolution_status)
    stays in `warnings` only, for developer diagnostics.

    A matched-but-merely-AMBIGUOUS entity (real candidates that just didn't
    clear the auto-select confidence/margin gate — a genuine, answerable
    tie, per _is_confirmed_dead_end's own docstring) is NOT unsupported and
    is deliberately left alone here: that case already flows to the ordinary
    clarification path elsewhere (context_builder._extract_ambiguous_terms),
    which already presents business-labeled options, not raw tables.

    Same aggregation/date/order/null-check exclusion as the other Day 3
    routing branches, and for the identical reason a regression surfaced
    live in this module's own test suite: a ranking/aggregate question
    ("Which recruiter has the most placements?") can match TWO taxonomy
    entity names in its text while still being fully answerable from the
    grounded entity's own table alone (the legacy measure/dimension
    resolution below never needed the unsupported one at all) — refusing
    outright here would override a question the existing, untouched legacy
    path already handles correctly.
    """
    if not grounding:
        return None
    if (
        query_intent.get("aggregation") is not None
        or query_intent.get("aggregation_target") is not None
        or query_intent.get("date_range")
        or query_intent.get("order")
        or query_intent.get("null_check_requested")
    ):
        return None
    matched = grounding.get("matched_entities") or []
    if not (2 <= len(matched) <= _MAX_RELATIONSHIP_ENTITIES):
        return None
    grounded = grounding.get("grounded") or {}
    unsupported = grounding.get("unsupported") or {}
    if not grounded or not unsupported:
        return None
    if not set(unsupported.keys()) <= set(matched) or not set(grounded.keys()) <= set(matched):
        return None

    warnings: list[dict] = []
    business_messages: list[str] = []
    for entity_name, contract in unsupported.items():
        evidence = ((contract.get("contract") or {}).get("evidence")) or []
        business_messages.append(_unsupported_entity_message(entity_name, contract))
        warnings.append({
            "type": "unsupported_entity", "severity": "HIGH",
            "message": (
                f"'{entity_name}' has no verified data model in this source "
                f"(status={contract.get('resolution_status')})."
                + (f" {evidence[0]}" if evidence else "")
            ),
        })

    preserved = sorted(grounded.keys())
    business_messages.append(
        "The rest of your question — " + ", ".join(preserved) + " — was understood; "
        "only the concept(s) named above are missing a verified data source."
    )

    intent = _build_intent(question, [], [], query_intent, [])
    return {
        "source_id": source_id,
        "intent": intent,
        "tables": [],
        "columns": {},
        "measures": [],
        "dimensions": [],
        "concepts": [],
        "filters": [],
        "join_plan": {
            "required": False, "tables": [], "primary_table": None,
            "steps": [], "fanout_risk": None, "confidence": 100,
        },
        "warnings": warnings,
        "confidence": 0,
        "explanation": [w["message"] for w in warnings],
        "candidate_ranking": [],
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": [],
        "entity": None, "entity_table": None,
        "entity_confidence": None, "entity_candidates": [],
        "unsupported_entities": sorted(unsupported.keys()),
        "grounded_entities": preserved,
        "business_messages": business_messages,
    }


def _compute_confidence(measures: list[dict], dimensions: list[dict], join_plan: dict, warnings: list[dict]) -> int:
    scores = [m["selected"]["score"] for m in measures if m["selected"]]
    scores += [d["selected"]["score"] for d in dimensions if d["selected"]]
    join_conf = join_plan.get("confidence", 100) / 100.0

    if scores:
        combined = (sum(scores) / len(scores)) * 0.7 + join_conf * 0.3
    else:
        combined = join_conf * 0.3

    high_severity = sum(1 for w in warnings if w.get("severity") == "HIGH")
    penalty = min(0.4, 0.15 * high_severity)
    return max(0, int(round((combined - penalty) * 100)))


def _build_explanation(measures: list[dict], dimensions: list[dict], join_plan: dict, warnings: list[dict]) -> str:
    parts: list[str] = []
    sel_measures = [m["selected"] for m in measures if m["selected"]]
    sel_dims = [d["selected"] for d in dimensions if d["selected"]]

    if sel_measures:
        parts.append("Measures: " + ", ".join(f"{m['table_fqn']}.{m['column_name']}" for m in sel_measures) + ".")
    if sel_dims:
        parts.append("Dimensions: " + ", ".join(f"{d['table_fqn']}.{d['column_name']}" for d in sel_dims) + ".")
    if join_plan.get("required"):
        if all(s.get("path_found") for s in join_plan.get("steps", [])):
            parts.append(f"Join required across {len(join_plan['tables'])} table(s).")
        else:
            parts.append("Join required but no trusted path was found for at least one table pair.")
    if warnings:
        parts.append(f"{len(warnings)} warning(s) — review before generating SQL.")
    if not parts:
        parts.append("No measures or dimensions could be confidently resolved from this request.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Enterprise Accuracy Program A2/Phase C — Compound Business Phrase Resolution
#
# Additive orchestration only: _resolve_term/_resolve_entity_count/
# _resolve_concept and their shared _AUTO_SELECT_MIN_CONFIDENCE/
# _AMBIGUITY_MARGIN gate are not modified. This helper only decides WHAT
# STRING to resolve first — a bounded adjacent-phrase candidate
# (core.semantic.concept_resolver.generate_compound_phrase_candidates) tried
# through the caller's own resolver_fn before the original per-token terms.
# A compound is accepted only when resolver_fn itself (unmodified) already
# returns a confident, non-ambiguous "selected" for it — the exact same gate
# every single-token term already goes through today.
# ---------------------------------------------------------------------------

def _resolve_role_with_compound_preference(
    terms: list[str], question: str, source_id: int, resolver_fn, budget: list[int], *, session=None,
) -> list[dict]:
    """
    Try each bounded adjacent-phrase candidate from `terms` through
    `resolver_fn` (a term -> resolved-entry callable — the same
    _resolve_term/_resolve_entity_count/_resolve_concept partial the caller
    already uses today) BEFORE resolving individual tokens. A candidate is
    accepted only when resolver_fn's own, unmodified gate already returned a
    non-null `selected`. Before resolving, the compound string is passed
    through the same remembered-terminology substitution
    (get_synonym_canonical) already applied to single tokens above in
    plan_business_query, so a human-taught synonym for the compound itself
    is honored too.

    On acceptance, both component tokens are removed from this role's
    remaining terms (suppressed only for THIS role's list — a token consumed
    by a compound here can still independently resolve in a different role's
    list, e.g. concepts vs measures). Candidates are tried left-to-right;
    once a token is consumed, no later candidate in this same role may reuse
    it (no overlapping compounds).

    Every term not consumed by an accepted compound is resolved individually
    via resolver_fn, in original order — identical to the plain
    `[resolver_fn(t) for t in terms]` list comprehension this replaces
    whenever no compound is generated or none resolves confidently. `budget`
    is a shared mutable [remaining] counter across all three roles
    (measures/dimensions/concepts) in one plan_business_query call, so the
    total number of compound-resolution attempts for one question stays
    bounded regardless of how many role lists contain adjacent pairs.
    """
    consumed: set[str] = set()
    resolved: dict[str, dict] = {}
    if len(terms) > 1 and budget[0] > 0:
        for candidate in generate_compound_phrase_candidates(question, terms):
            first, second = candidate["components"]
            if first in consumed or second in consumed:
                continue
            if budget[0] <= 0:
                break
            budget[0] -= 1
            phrase = get_synonym_canonical(source_id, candidate["phrase"], session=session) or candidate["phrase"]
            entry = resolver_fn(phrase)
            if entry.get("selected") is not None:
                consumed.add(first)
                consumed.add(second)
                resolved[first] = entry

    ordered: list[dict] = []
    emitted_compound_terms: set[str] = set()
    for t in terms:
        if t in consumed:
            if t in resolved and t not in emitted_compound_terms:
                ordered.append(resolved[t])
                emitted_compound_terms.add(t)
            continue
        ordered.append(resolver_fn(t))
    return ordered


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan_business_query(source_id: int, user_id: str, request: dict, *, session=None) -> dict | None:
    """
    Step 2. Compose a safe query plan from a structured business request.

    request = {"question": str, "concepts": [...], "measures": [...],
               "dimensions": [...], "filters": [...]}  (all keys optional
               except question; missing keys default to empty).

    Never generates SQL, never executes anything, never invents a table or
    column that wasn't returned by find_business_assets / get_table_business_context.

    Returns None when the source does not exist or is not owned by user_id.

    Phase 3.2A — thin wrapper around _plan_business_query_impl so the
    request-scoped RequestMetadataSession this function opens (one
    connection reused for the whole planning request — see
    data.request_metadata_session) is guaranteed to close via `with`,
    including on exception, without reindenting the existing ~500-line
    implementation. A caller that already has a session (e.g. one shared
    across plan -> build -> generate for a single question) passes it in
    and owns its lifecycle instead.
    """
    if session is not None:
        return _plan_business_query_impl(source_id, user_id, request, session=session)
    with RequestMetadataSession(source_id, user_id) as owned_session:
        return _plan_business_query_impl(source_id, user_id, request, session=owned_session)


def _plan_business_query_impl(source_id: int, user_id: str, request: dict, *, session) -> dict | None:
    conn = session.conn
    if not _verify_source(conn, source_id, user_id):
        return None

    question        = (request.get("question") or "").strip()
    concepts        = list(dict.fromkeys(request.get("concepts") or []))
    measure_terms   = list(dict.fromkeys(request.get("measures") or []))
    dimension_terms = list(dict.fromkeys(request.get("dimensions") or []))
    filters         = request.get("filters") or []

    # Phase 3, Step 2 — remembered-synonym resolution: if a human has
    # previously taught this source that a term means the same thing as
    # another term (concept_mapping_service.remember_synonym), substitute
    # the canonical term here, once, before anything below (candidate-table
    # retrieval, concept-mapping bonus scoring, column resolution) ever sees
    # the original term. A term with no remembered synonym passes through
    # unchanged — get_synonym_canonical returns None for it, so behavior is
    # byte-for-byte identical to today for every source with nothing taught.
    #
    # Phase 3, Step 4 — captured before substitution overwrites these lists,
    # so the raw (pre-substitution) term is preserved alongside the
    # canonical term it resolved to, purely for explainability; merged with
    # semantic_retrieval_service's own copy (from resolving all_terms below)
    # once retrieval returns.
    remembered_terminology = _remembered_terminology_evidence(
        source_id, concepts + measure_terms + dimension_terms, session=session,
    )
    concepts        = list(dict.fromkeys(get_synonym_canonical(source_id, t, session=session) or t for t in concepts))
    measure_terms   = list(dict.fromkeys(get_synonym_canonical(source_id, t, session=session) or t for t in measure_terms))
    dimension_terms = list(dict.fromkeys(get_synonym_canonical(source_id, t, session=session) or t for t in dimension_terms))

    query_intent = extract_query_intent(question)

    # Enterprise Implementation Phase 2 — Analytics Intent Layer. Recognizes
    # the BUSINESS QUESTION SHAPE (ranking/trend/comparison/distribution/
    # aggregation) before any column resolution happens below. Only overrides
    # the caller's concepts/measures/dimensions when ALL of:
    #   (1) confidence == "high" (both an entity and a measure were found);
    #   (2) shape_source is "wh_ranking" or "trend" — NOT "top_n_by".
    #       "Top N <entity> by <measure>" ("Top 10 clients by active jobs")
    #       is genuinely ambiguous with "Top 10 clients by revenue" (same
    #       surface phrasing, but "revenue" is already a column on the
    #       clients table itself — no grouping, order by that column
    #       directly, per the existing, tested
    #       test_top_n_order_resolved_to_measure_column behavior). Telling
    #       the two apart requires knowing whether the measure resolves to
    #       a column on the entity's own table or a related one — that's
    #       one stage later than this layer runs, so "top_n_by" is left
    #       unresolved here rather than guessed at; see
    #       derive_analytics_intent's own shape_source docs;
    #   (3) the caller didn't already supply more than one measure/dimension
    #       term — never silently drops extra terms a caller explicitly
    #       resolved.
    # wh_ranking fixes a real gap: "highest"/"most" default to counting
    # business records (COUNT), not a scalar MAX/MIN of a stored column,
    # unless the measure phrase itself names an explicit SUM/AVG/COUNT.
    analytics_intent = derive_analytics_intent(question)
    if (
        analytics_intent["confidence"] == "high"
        and analytics_intent["shape_source"] in ("wh_ranking", "trend")
        and len(measure_terms) <= 1 and len(dimension_terms) <= 1
    ):
        if analytics_intent["measure"]:
            measure_terms = [analytics_intent["measure"]]
        if analytics_intent["entity"]:
            dimension_terms = [analytics_intent["entity"]]
        concepts = list(dict.fromkeys(
            t for t in (analytics_intent["entity"], analytics_intent["measure"]) if t
        ))
        query_intent = {
            **query_intent,
            "aggregation":        analytics_intent["aggregation"],
            "aggregation_target": analytics_intent["aggregation_target"],
            "order": (
                {"direction": analytics_intent["ordering"], "limit": analytics_intent["top_n"]}
                if analytics_intent["ordering"] and not query_intent["order"]
                else query_intent["order"]
            ),
        }

    all_terms = list(dict.fromkeys(concepts + measure_terms + dimension_terms))

    # ---------------------------------------------------------------------
    # Day 2B, Task 3 — Automatic Business Semantic Grounding. Matches this
    # question's deterministic terms against the fixed target-entity
    # taxonomy (data.semantic_contract_service) and loads each matched
    # entity's already-persisted (or freshly, automatically built —
    # get_or_build_entity_contract, no approval step) contract. Real CCPP
    # discovery showed grounding is often PARTIAL across a multi-entity
    # question, so this never gates all-or-nothing — see the two branches
    # below (full skip of broad retrieval vs. additive seeding) and
    # apply_grounding's own docstring.
    #
    # Day 2E, Task 3 — moved before the Single-Object Sufficiency Check
    # below (previously ran after it, meaning that check was completely
    # contract-blind — a genuine, unrelated source of non-determinism: the
    # same status-enumeration question could be answered via this contract-
    # aware path on one call and via that independent, broad-search-backed
    # mechanism on another, live-confirmed to occasionally disagree).
    # ---------------------------------------------------------------------
    grounding: dict | None = None
    grounding_duration_ms: float | None = None
    if all_terms:
        from data.semantic_contract_service import apply_grounding
        _grounding_t0 = time.monotonic()
        try:
            grounding = apply_grounding(
                source_id, user_id, question, concepts, measure_terms, dimension_terms, session=session,
            )
        except MetadataSearchFailedError as exc:
            # Grounding is an additive optimization — a genuine
            # infrastructure failure inside its own bounded retrieval call
            # (same failure mode _get_ai_candidate_tables_ranked already
            # guards against below) must degrade to "no grounding" and let
            # the existing broad-search path run/fail on its own terms,
            # never crash the whole planning call outright.
            logger.warning(
                "plan_business_query: semantic grounding retrieval failed for "
                "source_id=%s entity terms=%s: %s — falling back to ungrounded planning",
                source_id, all_terms, exc,
            )
            grounding = None
        grounding_duration_ms = (time.monotonic() - _grounding_t0) * 1000
        from core.perf import stage_timer

        stage_timer.record("semantic_grounding", grounding_duration_ms)
        if grounding and grounding["matched_entities"]:
            logger.info(
                "plan_business_query: semantic grounding for source_id=%s matched=%s "
                "fully_grounded=%s took %.1fms",
                source_id, grounding["matched_entities"], grounding["fully_grounded"], grounding_duration_ms,
            )

    # Phase 1 Stabilization — Single-Object Sufficiency Check. A status/
    # category ENUMERATION question ("...are stalled, active, graduated, or
    # not started?") can never be resolved by ordinary term-to-column-name
    # matching (see _find_sufficient_single_object's own docstring) and
    # previously fell through to an expensive, doomed multi-table/join
    # search. Checked here, before any of that runs, and only for this
    # specific question shape (>=2 enumerated status literals) — every
    # other question shape is completely unaffected. grounding (now already
    # computed above) is threaded through so a grounded entity's own
    # contract object is used directly (Day 2E, Task 3) instead of this
    # check's own independent, broad-search-backed discovery.
    status_values = query_intent.get("status_values") or []
    if len(status_values) >= 2:
        entity_terms = concepts or measure_terms
        sufficient = _find_sufficient_single_object(
            entity_terms, question, status_values, source_id, user_id, grounding=grounding, session=session,
        )
        if sufficient:
            return _build_sufficient_object_plan(
                source_id, user_id, question, entity_terms,
                sufficient, query_intent, remembered_terminology,
            )

    warnings: list[dict] = []
    if not all_terms:
        warnings.append({
            "type": "no_search_terms", "severity": "MEDIUM",
            "message": "No concepts, measures, or dimensions were provided to plan against.",
        })

    # Day 2D, Priority 2 — fast exit for unsupported entities. Only when
    # NOTHING at all grounded (grounding["grounded"] empty) and every
    # matched entity is a confirmed-unsupported contract: this is exactly
    # what protects a mixed question ("candidates linked to active
    # placements") from being fast-exited just because Placement has no
    # contract — Candidate grounding fine means grounded is non-empty, so
    # this never fires and the ordinary (partial-grounding) path runs
    # unaffected. Must run before the broad-retrieval block below — that
    # block's own cost (a multi-query search_metadata scan) is exactly what
    # this skips.
    #
    # Day 2E, Task 4 — excluded when the question is aggregating a stored
    # metric column (measure_sum/average/min/max — "Total client count").
    # That resolves entirely at the COLUMN level (_resolve_term against a
    # real, already-hydrated candidate table) and never needs the matched
    # entity to resolve as a TABLE at all; a taxonomy word merely
    # co-occurring with a real, summable column must never block summing
    # it. entity_count/distinct_entity_count ("how many clients") are
    # unaffected by this exclusion — counting entities genuinely does need
    # the entity to resolve as a table, so a confirmed-unsupported one
    # correctly still fast-exits there (a targeted refusal, per Task 5,
    # rather than a single-weak-candidate physical-table picker).
    if (
        grounding and grounding["matched_entities"] and not grounding["grounded"]
        and len(grounding.get("unsupported") or {}) == len(grounding["matched_entities"])
        and query_intent.get("aggregation_target") not in (
            "measure_sum", "measure_average", "measure_min", "measure_max",
        )
    ):
        return _build_unsupported_entity_plan(source_id, question, grounding, query_intent, remembered_terminology)

    # Day 3, Task 6 — targeted business refusal for a PARTIALLY grounded
    # multi-entity question (some entities grounded, at least one other
    # confirmed-unsupported — the mixed case the all-unsupported fast-exit
    # just above deliberately excludes). Must run here, before the broad-
    # retrieval block below, for the same reason as the fast-exit above:
    # letting broad search run first would either waste its cost only to
    # refuse anyway, or — worse — let it turn up a weak, unrelated candidate
    # for the unsupported entity and surface a physical-table clarification
    # for something already known to have no verified data model. See
    # _try_build_partial_grounding_refusal's own docstring for the exact
    # gate (aggregation/date/order/null-check questions are excluded so a
    # ranking question that only ever needed the grounded entity's own
    # table, e.g. "Which recruiter has the most placements?", is untouched).
    partial_refusal_plan = _try_build_partial_grounding_refusal(
        source_id, question, grounding, query_intent, remembered_terminology,
    )
    if partial_refusal_plan is not None:
        return partial_refusal_plan

    # Phase 3.2 / Task 6 — distinct from "no candidates found": an
    # infrastructure failure inside retrieval (MemoryError, a broken
    # connection) now raises MetadataSearchFailedError instead of being
    # silently swallowed into an empty candidate set. Caught here so the
    # rest of this function's existing "empty candidate_tables -> qtype
    # unresolved" degradation still applies (nothing downstream needs to
    # change to stay safe), while metadata_search_failed on the returned
    # dict lets a caller that cares distinguish the two cases instead of
    # being told "no data found" when the real story is "the search
    # subsystem broke".
    metadata_search_failed = False
    metadata_search_failed_reason: str | None = None

    # Semantic Retrieval Integration: bounded retrieval only. An empty result
    # (weak/ambiguous domain, no search_metadata match, or a retrieval
    # failure — get_candidate_tables() never raises) is NOT a trigger to
    # fall back to the unbounded, un-domain-filtered _collect_candidate_tables()
    # scan. It flows through as an empty candidate set, which the existing
    # _build_intent() logic below already turns into a safe qtype="unresolved"
    # plan (no measures/dimensions resolved) rather than any full-schema scan.
    # candidate_ranking (Phase 2, Step 3) is the scored, capped ranking
    # behind candidate_tables — carried into the returned query_plan below
    # purely as evidence for downstream consumers (e.g. presenting "here's
    # what was found but not confidently picked" for clarification). It
    # never changes which tables are selected; selection is unchanged from
    # get_candidate_tables()'s prior behavior.
    if all_terms:
        # Enterprise Accuracy Program A2/Phase C — additively widen retrieval
        # with the same bounded adjacent-phrase candidates the resolution
        # stage below will try (e.g. "job orders" alongside "job"/"orders"),
        # so a table only strongly matched by the compound string isn't
        # excluded from table_contexts before resolution ever sees it.
        # Purely additive: all_terms itself (used by the no_search_terms
        # check above and untouched elsewhere) is not modified, and
        # _get_ai_candidate_tables_ranked's own union/cap behavior is
        # unchanged — this only gives it more terms to search with.
        compound_phrases_for_retrieval = list(dict.fromkeys(
            candidate["phrase"]
            for role_terms in (concepts, measure_terms, dimension_terms)
            for candidate in generate_compound_phrase_candidates(question, role_terms)
        ))[:_COMPOUND_MAX_CANDIDATES_PER_QUESTION]
        retrieval_terms = list(dict.fromkeys(all_terms + compound_phrases_for_retrieval))
        if grounding and grounding["fully_grounded"]:
            # Day 2B, Task 3/6 — every entity this question names already
            # has a verified contract: skip the broad, multi-query
            # search_metadata retrieval entirely (the dominant cost this
            # stage exists to save) and seed candidates straight from the
            # contract(s). Everything downstream — context hydration,
            # _resolve_term/_resolve_concept/_resolve_entity_count,
            # _build_intent, Day 2A's status-filter bounded retry,
            # _plan_joins — runs completely unchanged against this smaller,
            # pre-vetted candidate set, so a generated-vocabulary mapping
            # never gets a chance to override a semantic contract: the
            # broad search that folds generated_business_vocabulary in is
            # the thing being skipped.
            candidate_tables = set(grounding["seed_table_fqns"])
            candidate_ranking = [
                {"qualified_name": fqn, "relevance_score": 100000.0, "source": "semantic_contract"}
                for fqn in candidate_tables
            ]
            retrieval_remembered_terminology = []
        else:
            try:
                candidate_tables, candidate_ranking, retrieval_remembered_terminology = (
                    _get_ai_candidate_tables_ranked(
                        source_id, user_id, question, retrieval_terms, session=session,
                    )
                )
            except MetadataSearchFailedError as exc:
                logger.error(
                    "plan_business_query: metadata search infrastructure failed for "
                    "source_id=%s terms=%s: %s", source_id, all_terms, exc,
                )
                metadata_search_failed = True
                metadata_search_failed_reason = str(exc)
                candidate_tables, candidate_ranking, retrieval_remembered_terminology = set(), [], []
            if grounding and grounding["seed_table_fqns"]:
                # Task 3/5 — partial grounding: add whatever entities DID
                # resolve confidently as extra trusted seeds, additive only
                # — never removes anything the broad search itself found.
                candidate_tables = set(candidate_tables) | grounding["seed_table_fqns"]
        # Phase 3, Step 4 — merges this function's own copy (captured above,
        # before substitution) with semantic_retrieval_service's — see
        # _merge_remembered_terminology's docstring for why both can fire
        # for the same term and why a plain concatenation would duplicate.
        remembered_terminology = _merge_remembered_terminology(
            remembered_terminology, retrieval_remembered_terminology,
        )
        if not candidate_tables and not metadata_search_failed:
            logger.info(
                "plan_business_query: bounded retrieval found no candidate "
                "tables for source_id=%s terms=%s; returning unresolved "
                "instead of falling back to the unbounded scan",
                source_id, all_terms,
            )
    else:
        candidate_tables = set()
        candidate_ranking = []

    # Phase 3.2A / Task 4 — batched candidate hydration: one IN-clause query
    # per metadata category for every candidate table, instead of the
    # per-candidate N+1 loop this used to be (get_table_business_context
    # once per fqn, ~9 queries each — 16 candidates was 16 connections and
    # ~144 queries on a real CCPP question). Same data, same shape, same
    # `if ctx:` truthiness filter — see
    # data.business_knowledge_service.get_table_business_contexts_batch's
    # own field-for-field equivalence tests.
    table_contexts: dict[str, dict] = {}
    for fqn, ctx in get_table_business_contexts_batch(
        source_id, user_id, candidate_tables or set(), session=session,
    ).items():
        if ctx:
            table_contexts[fqn] = ctx

    # Phase 2, Step 7 — one bulk read for the whole source (not one per
    # term/resolver call), reused across every _resolve_term/_resolve_concept/
    # _resolve_count_all call below via _concept_mapping_lookup's in-memory
    # filtering. Only fetched when there's something to resolve.
    concept_mappings = get_all_approved_mappings(source_id, session=session) if table_contexts else {}

    # Enterprise Phase 4 — same bulk-read-once-per-call discipline as
    # concept_mappings above, reused via _generated_vocab_lookup's in-memory
    # filtering across every _resolve_term/_resolve_concept/_resolve_count_all
    # call below.
    generated_vocab = get_generated_vocabulary(source_id, session=session) if table_contexts else {}

    # Milestone Phase 6.2 — Aggregation Shape Correctness. An entity-count
    # question ("how many students", "number of clients") must count
    # STUDENT/CLIENT RECORDS, not whatever metric column happens to
    # name-match the term (e.g. "students" substring-matching a stored
    # TotalStudents rollup) — so aggregation_target routes measure-term
    # resolution to _resolve_entity_count instead of column-level
    # _resolve_term entirely, rather than only falling back to bare
    # COUNT(*) after a (possibly wrong) column match already won. This
    # replaces the milestone M-1 "COUNT fallback" block that used to run
    # here — same underlying _resolve_count_all call, correctly promoted
    # from a last-resort fallback to the primary path for these targets.
    # measure_sum/measure_average/measure_min/measure_max keep the
    # unchanged _resolve_term column-level path — existing SUM/AVG/MIN/MAX
    # behavior is untouched.
    # Enterprise Accuracy Program A2/Phase C — one shared, bounded budget for
    # every compound-phrase resolution attempt across all three roles below
    # (see _resolve_role_with_compound_preference's own docstring).
    compound_budget = [_COMPOUND_MAX_CANDIDATES_PER_QUESTION]

    # A5 Milestone 2.1 — Suppress Redundant Measure Resolution for Bare
    # Entity Questions. extract_terms() puts a term with no "by"-clause into
    # BOTH concepts and measures (its own "before = before+after" contract),
    # so a pure entity question ("Show active clients") also runs a
    # column-level measure search for the identical term — meaningless when
    # nothing is being aggregated. That second search can silently win with
    # the wrong table (a coincidental column/name match overriding the
    # correct concept resolution) or spuriously flag "ambiguous_measure"
    # and block an already-resolved concept behind an irrelevant
    # clarification prompt. Reuses only already-computed query_intent/
    # concepts/measure_terms — no new metadata read, no threshold change.
    # Narrow by construction: any aggregation, any ranking/order, any
    # "by"-clause (which yields >=2 concepts), or any multi-term question
    # leaves this False and the existing measure-resolution path byte-for-
    # byte unchanged. See A5 M2 audit (2026-07-31) for the full evidence.
    bare_entity_term = (
        query_intent.get("aggregation") is None
        and query_intent.get("aggregation_target") is None
        and query_intent.get("order") is None
        and len(concepts) == 1
        and len(measure_terms) == 1
        and measure_terms[0] == concepts[0]
    )
    measure_terms_for_resolution = [] if bare_entity_term else measure_terms

    aggregation_target = query_intent.get("aggregation_target")
    if aggregation_target in ("entity_count", "distinct_entity_count"):
        measures = _resolve_role_with_compound_preference(
            measure_terms_for_resolution, question, source_id,
            resolver_fn=lambda t: _resolve_entity_count(
                t, table_contexts, distinct_requested=(aggregation_target == "distinct_entity_count"),
                concept_mappings=concept_mappings, generated_vocab=generated_vocab, grounding=grounding,
            ),
            budget=compound_budget, session=session,
        )
    else:
        measures = _resolve_role_with_compound_preference(
            measure_terms_for_resolution, question, source_id,
            resolver_fn=lambda t: _resolve_term(
                t, table_contexts, "measure", concept_mappings, generated_vocab, grounding=grounding,
            ),
            budget=compound_budget, session=session,
        )
    dimensions = _resolve_role_with_compound_preference(
        dimension_terms, question, source_id,
        resolver_fn=lambda t: _resolve_term(
            t, table_contexts, "dimension", concept_mappings, generated_vocab, grounding=grounding,
        ),
        budget=compound_budget, session=session,
    )
    resolved_concepts = _resolve_role_with_compound_preference(
        concepts, question, source_id,
        resolver_fn=lambda t: _resolve_concept(t, table_contexts, concept_mappings, generated_vocab, grounding=grounding),
        budget=compound_budget, session=session,
    )

    # Day 2E, Task 2 — global token consumption (post-resolution). Once a
    # grounded entity's own name has been successfully resolved as a
    # CONCEPT (e.g. the compound "launch participants" -> canonical table),
    # its individual name-WORDS must not also linger as separate, UNRESOLVED
    # measure/dimension entries — extract_terms()'s "before = before+after"
    # contract puts every bare content word into both concepts and measures,
    # so "launch"/"participants" independently re-appear as ambiguous/
    # missing measure entries even though the question they belong to was
    # already answered via the concept role (confirmed live). Deliberately
    # POST-resolution and conditioned on the entity's OWN concept having
    # actually resolved — never pre-filters the term lists themselves, so a
    # legitimate compound measure that happens to start with an entity-name
    # word (e.g. "invoice amount" naming a real invoice_amount column, with
    # "Invoice" separately grounded) is completely unaffected: the compound
    # already claimed "invoice"/"amount" as ONE resolved measure entry
    # before this ever runs, and an entity-count question's own measure
    # entry (e.g. "students" -> COUNT(StudentID)) is never touched either,
    # since it already has `selected` populated. Only ever removes an entry
    # that is BOTH consumed by an entity's name AND still unresolved.
    _concept_resolved_entities = {
        entity_name
        for c in resolved_concepts if c.get("selected")
        for entity_name in (grounding.get("grounded") or {}) if grounding
        if _term_names_grounded_entity((c.get("term") or "").lower(), entity_name)
    }
    if _concept_resolved_entities:
        from data.semantic_contract_service import entity_consumed_terms
        _consumed: set[str] = set()
        for _entity_name in _concept_resolved_entities:
            _consumed |= entity_consumed_terms(
                _entity_name, [m["term"] for m in measures] + [d["term"] for d in dimensions],
            )
        measures = [m for m in measures if not (m["term"] in _consumed and not m.get("selected"))]
        dimensions = [d for d in dimensions if not (d["term"] in _consumed and not d.get("selected"))]

    # Calendar-grain ("each year") / related-table-attribute ("names of the
    # students") dimension fallbacks — only for a dimension term ordinary
    # _resolve_term left unresolved (never overrides a genuine column
    # match). Anchored on whichever table a measure or the bare concept
    # already resolved to, since neither fallback ever invents a table of
    # its own.
    _primary_table_hint = (
        next((m["selected"]["table_fqn"] for m in measures if m.get("selected")), None)
        or next((c["selected"]["table_fqn"] for c in resolved_concepts if c.get("selected")), None)
    )
    if _primary_table_hint:
        _grain_hint_terms = [t for t in (concepts + measure_terms) if t not in _CALENDAR_GRAIN_TERMS]
        _fallback_dimensions: list[dict] = []
        for d in dimensions:
            if d.get("selected"):
                _fallback_dimensions.append(d)
            elif d["term"] in _CALENDAR_GRAIN_TERMS and _primary_table_hint in table_contexts:
                _fallback_dimensions.append(
                    _resolve_calendar_grain_dimension(
                        d["term"], _primary_table_hint, table_contexts[_primary_table_hint], _grain_hint_terms,
                    )
                )
            else:
                _related = _resolve_related_attribute_dimension(d["term"], _primary_table_hint, source_id, user_id)
                _fallback_dimensions.extend(_related if _related else [d])
        dimensions = _fallback_dimensions

    # Enterprise Phase 4 — pure explainability, mirrors remembered_terminology's
    # own dedup-by-pair pattern (_merge_remembered_terminology): one record per
    # (original_term, generated_term) pair for which a generated-vocabulary
    # bonus actually decided the SELECTED candidate, never for every scored
    # candidate. Feeds core/answering/citation_builder.py's
    # _cite_generated_vocabulary — never affects selection itself.
    generated_vocabulary_evidence: list[dict] = []
    _seen_generated_vocab_pairs: set[tuple[str, str]] = set()
    for entry in measures + dimensions + resolved_concepts:
        selected = entry.get("selected")
        generated_term = selected.get("generated_vocabulary_term") if selected else None
        if not generated_term:
            continue
        pair = (entry["term"], generated_term)
        if pair in _seen_generated_vocab_pairs:
            continue
        _seen_generated_vocab_pairs.add(pair)
        generated_vocabulary_evidence.append({
            "original_term": entry["term"],
            "generated_term": generated_term,
            "source": "generated_vocabulary",
        })

    for entry in measures + dimensions:
        warnings.extend(entry["warnings"])

    # Day 3 — Multi-Entity Relationship Questions. Tried before the legacy
    # selected_tables/_build_intent path immediately below (which is
    # unmodified and still runs, unchanged, for every question neither
    # branch claims). The aggregate branch (Task 4) is tried before the
    # bare-list branch (Tasks 1/2/3/5) since an aggregation question also
    # satisfies the list branch's own gate whenever it has no explicit
    # status filter, and the aggregate-specific handling is the correct one.
    # See each function's own docstring for its exact, narrow gate.
    #
    # Task 6 (partial-grounding refusal) already ran much earlier in this
    # function, right after the pre-existing all-unsupported fast-exit and
    # before the broad-retrieval block — a question this dict comprehension
    # would otherwise still be building never reaches this point at all.
    relationship_aggregate_plan = _try_build_relationship_aggregate_plan(
        source_id, user_id, question, grounding, query_intent, table_contexts, measure_terms,
        candidate_ranking, remembered_terminology, generated_vocabulary_evidence, warnings,
        concept_mappings, generated_vocab,
    )
    if relationship_aggregate_plan is not None:
        return relationship_aggregate_plan

    relationship_plan = _try_build_relationship_plan(
        source_id, user_id, question, grounding, query_intent, table_contexts,
        candidate_ranking, remembered_terminology, generated_vocabulary_evidence, warnings,
        resolved_concepts,
    )
    if relationship_plan is not None:
        return relationship_plan

    selected_tables: set[str] = set()
    primary_table = None
    for m in measures:
        if m["selected"]:
            selected_tables.add(m["selected"]["table_fqn"])
            if primary_table is None:
                primary_table = m["selected"]["table_fqn"]
    for d in dimensions:
        if d["selected"]:
            selected_tables.add(d["selected"]["table_fqn"])

    # Bare-entity questions (list_entities/joined_detail_list — "students",
    # "names of the students") resolve no measure at all, so primary_table
    # stayed unset here even though the concept itself resolved confidently
    # — leaving date/status/order column discovery below to search the
    # FULL, unscoped candidate set (table_contexts) instead of the resolved
    # entity's own table, and letting it pick up an unrelated table's date
    # column (e.g. a junction table's own timestamp) for "most recently
    # added students" ordering. Anchoring on the resolved concept's table
    # here is strictly a narrowing, never a behavior change for a question
    # where nothing resolved at all (primary_table stays None exactly as
    # before in that case).
    if primary_table is None:
        # Day 2A, Task 4 — before anchoring on the resolved concept's table,
        # give it one bounded retry against a compatible status candidate
        # when a status filter was requested (see
        # _resolve_status_compatible_entity's own docstring). No-op for any
        # question without a status_value, and a no-op when the top
        # candidate is already compatible — never changes behavior outside
        # its narrow trigger condition.
        _resolve_status_compatible_entity(
            resolved_concepts, table_contexts, query_intent.get("status_value"), source_id, session=session,
        )
        _concept_hit = next((c["selected"]["table_fqn"] for c in resolved_concepts if c.get("selected")), None)
        if _concept_hit:
            primary_table = _concept_hit
            selected_tables.add(_concept_hit)

    join_plan = _plan_joins(source_id, user_id, primary_table, selected_tables)
    _apply_join_fanout_safety(measures, join_plan, warnings)
    warnings.extend(_collect_governance_warnings(table_contexts, measures, dimensions, join_plan))

    # Date/status column discovery below must only search tables that are
    # actually part of the join graph (join_plan["tables"]) — not every
    # semantically-retrieved candidate table in table_contexts. Searching
    # the full candidate set can find a date/status column on a table that
    # was never added to selected_tables/the join plan, producing a filter
    # that references a table outside FROM/JOIN (M-19's plan-integrity
    # guard then correctly refuses rather than silently dropping it or
    # auto-joining an unrelated table).
    #
    # Exception: if no measure/dimension resolved at all (selected_tables
    # and primary_table both empty), there is no join graph yet to violate
    # — a bare filter-only question ("invoices this month" with no distinct
    # measure term) still needs to discover its date/status column from the
    # full candidate set, and whatever table it's found on simply becomes
    # the sole FROM table since nothing else was selected either.
    if selected_tables or primary_table:
        graph_table_fqns = set(join_plan["tables"]) if join_plan["tables"] else (
            {primary_table} if primary_table else set()
        )
        graph_table_contexts = {
            fqn: table_contexts[fqn] for fqn in graph_table_fqns if fqn in table_contexts
        }
    else:
        graph_table_contexts = table_contexts

    # Filters are validated against discovered columns only — never invented.
    known_column_names = {
        (fqn, c["column_name"]) for fqn, ctx in table_contexts.items() for c in ctx["columns"]
    }
    resolved_filters = []
    for f in filters:
        col_name = f.get("column") or f.get("field")
        match = next(((fqn, cn) for (fqn, cn) in known_column_names if cn == col_name), None) if col_name else None
        resolved_filters.append({
            **f,
            "resolved":  match is not None,
            "table_fqn": match[0] if match else None,
        })
        if col_name and match is None:
            warnings.append({
                "type": "unknown_filter_column", "severity": "LOW",
                "message": f"Filter references unknown column '{col_name}'.",
            })

    explicit_filter_columns = {f.get("column") or f.get("field") for f in resolved_filters}

    # Date-intelligence filter (today/this month/last quarter/between X and Y/...)
    # — only added when a real date column was discovered, and only when the
    # caller didn't already supply an explicit filter on that same column.
    if query_intent["date_range"] and graph_table_contexts:
        date_col = _find_date_column(graph_table_contexts, primary_table)
        if date_col:
            fqn, col_name = date_col
            if col_name not in explicit_filter_columns:
                resolved_filters.append({
                    "column": col_name, "operator": "BETWEEN",
                    "value": [query_intent["date_range"]["start"], query_intent["date_range"]["end"]],
                    "resolved": True, "table_fqn": fqn,
                })
        else:
            warnings.append({
                "type": "date_column_not_found", "severity": "LOW",
                "message": (
                    f"A '{query_intent['date_range']['label']}' date range was requested but no "
                    "date column was found among the resolved tables — no date filter applied."
                ),
            })

    # Status filter ("active students", "open jobs", ...) — same discover-or-warn rule.
    if query_intent["status_value"] and graph_table_contexts:
        # Day 2B, Task 4 — prefer a semantic contract's own VERIFIED status
        # column+value over the generic keyword-based finder below, when
        # one is available for a table actually in play: "map the status
        # word to an actual verified field/value" rather than a name-match
        # guess. Only used when the requested word is itself one of the
        # contract's own verified_values (or a recognized boolean word for
        # a BOOLEAN-typed column) — never a blind override.
        contract_status_col: tuple[str, str, object] | None = None
        if grounding:
            normalized_requested = query_intent["status_value"].lower()
            for hint_table_fqn, hint in grounding["status_hints"].items():
                if hint_table_fqn not in graph_table_contexts:
                    continue
                if hint["data_type"] == "BOOLEAN" and normalized_requested in _STATUS_BOOLEAN_VALUES:
                    contract_status_col = (
                        hint_table_fqn, hint["column_name"], _STATUS_BOOLEAN_VALUES[normalized_requested],
                    )
                    break
                matched_value = next(
                    (v for v in hint["verified_values"] if v.lower() == normalized_requested), None,
                )
                if matched_value is not None:
                    contract_status_col = (hint_table_fqn, hint["column_name"], matched_value)
                    break

        if contract_status_col:
            fqn, col_name, typed_value = contract_status_col
            status_col = (fqn, col_name)
        else:
            status_col = _find_status_column(graph_table_contexts, primary_table)
            typed_value = query_intent["status_value"]
        if status_col:
            fqn, col_name = status_col
            if col_name not in explicit_filter_columns:
                resolved_filters.append({
                    "column": col_name, "operator": "=", "value": typed_value,
                    "resolved": True, "table_fqn": fqn,
                })
        else:
            warnings.append({
                "type": "status_column_not_found", "severity": "LOW",
                "message": (
                    f"A status filter ('{query_intent['status_value']}') was requested but no status "
                    "column was found among the resolved tables — no status filter applied."
                ),
            })

    # "have a phone number on file" / "... on record" — Day 2C follow-up
    # ("material qualifier policy", Part B): the idiom's own words never
    # reach term search (extract_terms()/_IDIOM_PHRASES already strip
    # them), so the only remaining signal is query_intent["null_check_
    # requested"] plus whichever single measure/dimension term is left
    # over as the idiom's subject. Only auto-applied when EXACTLY ONE
    # already-resolved, real COLUMN (never a table-level entity-count
    # resolution — that counts an entity, not a nullable attribute, and
    # is deliberately left to the missing/ambiguous-term handling above
    # rather than guessed at here) is a candidate, so this never guesses
    # among several plausible attributes.
    if query_intent.get("null_check_requested") and graph_table_contexts:
        null_check_candidates = [
            entry for entry in (measures + dimensions)
            if entry.get("selected")
            and entry["selected"].get("column_name")
            and not entry["selected"].get("aggregation_target")
            and entry["selected"]["column_name"] not in explicit_filter_columns
        ]
        if len(null_check_candidates) == 1:
            sel = null_check_candidates[0]["selected"]
            resolved_filters.append({
                "column": sel["column_name"], "operator": "IS NOT NULL", "value": None,
                "resolved": True, "table_fqn": sel["table_fqn"],
            })

    # Ordering (Top N / Bottom N / Latest / Earliest) — resolve the target
    # column once here, in the semantic layer, exactly like measures/
    # dimensions/joins are already pre-resolved before SQL planning.
    resolved_order = None
    if query_intent["order"]:
        order = dict(query_intent["order"])
        if order.get("target") == "date":
            date_col = _find_date_column_with_hint(
                graph_table_contexts, primary_table, _date_order_hint_root(question),
            )
            if date_col:
                order["table_fqn"], order["column_name"] = date_col
                resolved_order = order
            else:
                warnings.append({
                    "type": "order_column_not_found", "severity": "LOW",
                    "message": "Latest/earliest ordering was requested but no date column was found — "
                               "results are limited but not sorted.",
                })
                resolved_order = {"direction": order["direction"], "limit": order["limit"]}
        else:
            resolved_order, order_warning = _resolve_ranking_order_column(order, measures)
            if order_warning:
                warnings.append(order_warning)

    intent = _build_intent(
        question, measures, dimensions, {**query_intent, "order": resolved_order}, resolved_concepts,
    )
    confidence = _compute_confidence(measures, dimensions, join_plan, warnings)
    explanation = _build_explanation(measures, dimensions, join_plan, warnings)

    # Entity-Centric Planner (A5 Milestone 1) — additive-only evidence
    # fields, projected from resolved_concepts (_resolve_concept's own
    # output, computed above — never a second resolution/scoring pass).
    # Populated with the SAME "bare entity" gate _build_intent itself uses
    # (no measure/dimension resolved, exactly one concept term extracted),
    # so these fields describe precisely the questions whose primary request
    # is a business entity rather than a metric — never populated for an
    # aggregate question where a measure/dimension independently resolved.
    entity = entity_table = entity_confidence = None
    entity_candidates: list[dict] = []
    if (
        not any(m["selected"] for m in measures)
        and not any(d["selected"] for d in dimensions)
        and len(resolved_concepts) == 1
    ):
        only_concept = resolved_concepts[0]
        entity = only_concept.get("term")
        entity_confidence = only_concept.get("confidence")
        entity_candidates = only_concept.get("candidates") or []
        if only_concept.get("selected"):
            entity_table = only_concept["selected"].get("table_fqn")

    return {
        "source_id":   source_id,
        "intent":      intent,
        "tables":      sorted(table_contexts.keys()),
        "columns":     {fqn: [c["column_name"] for c in ctx["columns"]] for fqn, ctx in table_contexts.items()},
        "measures":    measures,
        "dimensions":  dimensions,
        "concepts":    resolved_concepts,
        "filters":     resolved_filters,
        "join_plan":   join_plan,
        "warnings":    warnings,
        "confidence":  confidence,
        "explanation": explanation,
        "candidate_ranking": candidate_ranking,
        "remembered_terminology": remembered_terminology,
        "generated_vocabulary_evidence": generated_vocabulary_evidence,
        # Entity-Centric Planner (A5 Milestone 1) — additive only.
        "entity":            entity,
        "entity_table":      entity_table,
        "entity_confidence": entity_confidence,
        "entity_candidates": entity_candidates,
        # Phase 3.2 / Task 6 — distinct from "genuinely no candidates":
        # True only when metadata-search infrastructure itself failed (see
        # MetadataSearchFailedError above). qtype/tables/etc. above still
        # degrade exactly like the "no candidates" case for callers that
        # don't check this — it is additive, not a new required field.
        "metadata_search_failed":        metadata_search_failed,
        "metadata_search_failed_reason": metadata_search_failed_reason,
        # Day 2B, Task 6 — surfaced so core.orchestrator.agent can add a
        # trace step for grounding lookup/build without re-deriving it.
        # Deliberately excludes the wall-clock grounding_duration_ms (logged
        # separately below): query_plan is expected to be a reproducible
        # function of (source_id, request) — e.g. with vs. without a
        # RequestMetadataSession must produce byte-identical plans — and a
        # timing measurement doesn't belong in that contract.
        "grounding": (
            {
                "matched_entities": grounding["matched_entities"],
                "grounded_entities": list(grounding["grounded"].keys()),
                "fully_grounded": grounding["fully_grounded"],
                "seed_table_fqns": sorted(grounding["seed_table_fqns"]),
            } if grounding else None
        ),
    }
