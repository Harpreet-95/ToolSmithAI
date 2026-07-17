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

from data.db import get_connection
from data.knowledge_graph_service import find_business_assets, _compute_importance_score
from data.business_knowledge_service import get_table_business_context
from data.semantic_layer_service import analyze_join_quality, recommend_best_join_path
from core.dictionary.rule_classifier import _METRIC_TOKENS, _tokenize
from core.semantic.concept_resolver import extract_query_intent, derive_analytics_intent
from core.semantic.compatibility_guard import infer_term_family, check_compatibility
from data.vocabulary_service import expand_concept
from data.semantic_retrieval_service import get_candidate_tables as _get_ai_candidate_tables

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

def _score_term_match_single(term: str, *texts: str | None) -> float:
    """
    0-1 similarity between a search term and one or more candidate text
    fields (column name, business label, meaning). Token-overlap (Jaccard)
    with a substring bonus — deterministic and explainable.
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
        overlap = len(term_toks & toks) / len(term_toks | toks)
        score = overlap
        if term_lower in text_lower or text_lower in term_lower:
            score = max(score, 0.75)
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

def _score_table_authority(table_fqn: str, ctx: dict) -> dict:
    """
    0-1-ish bonus/penalty (clamped to [-0.5, 0.5]) plus human-readable reasons,
    derived entirely from fields get_table_business_context() already returns
    for `table_fqn` — no new reads, no invented metadata.
    """
    reasons: list[str] = []
    bonus = 0.0

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
        bonus += importance * 0.30
    if governance.get("dictionary_approved"):
        reasons.append("Dictionary Approved")
    if profiling and profiling.get("is_root_table"):
        reasons.append("Root/primary table")
    if profiling and profiling.get("table_class") in ("Master", "Reference"):
        reasons.append(f"{profiling['table_class']} table")

    if governance.get("domain_assigned"):
        bonus += 0.05
        reasons.append(f"Domain = {domain_row['domain']}")
    if governance.get("entity_assigned"):
        bonus += 0.07
        reasons.append(f"Entity = {entity_row['entity']}")

    rel_count = len(relationships.get("outbound") or []) + len(relationships.get("inbound") or [])
    if rel_count:
        bonus += min(0.12, 0.02 * rel_count)
        reasons.append(f"Relationship coverage ({rel_count} linked table(s))")

    if profiling:
        row_count = profiling.get("exact_row_count")
        if row_count is None:
            row_count = profiling.get("estimated_row_count")
        if row_count:
            bonus += min(0.15, 0.025 * math.log10(row_count + 1))
            reasons.append(f"Row count evidence ({row_count:,} rows)")
        elif row_count == 0:
            bonus -= 0.10
            reasons.append("Table is empty (0 rows)")

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

    return {"bonus": round(max(-0.5, min(0.5, bonus)), 4), "reasons": reasons}


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


def _score_candidates(term: str, table_fqn: str, columns: list[dict], predicate, table_authority: dict) -> list[dict]:
    out = []
    for col in columns:
        if not predicate(col):
            continue
        dic = col.get("dictionary") or {}
        prof = col.get("profiling") or {}
        name_score = _score_term_match(term, col["column_name"], dic.get("business_label"), dic.get("meaning"))
        score = max(0.0, min(1.0, name_score + table_authority["bonus"]))
        out.append({
            "table_fqn":        table_fqn,
            "column_name":      col["column_name"],
            "business_label":   dic.get("business_label"),
            "score":            score,
            "name_score":       name_score,
            "authority_bonus":  table_authority["bonus"],
            "ranking_reasons":  table_authority["reasons"],
            "is_approved":      bool(dic.get("is_approved")) if dic else False,
            "data_type":        (col.get("schema") or {}).get("data_type"),
            # Milestone Phase 6.1 — Semantic Correctness Guard: the column's
            # own already-computed profiling semantic type, carried through
            # so _resolve_term can check it against the requested term's
            # inferred concept family before ever auto-selecting this
            # candidate. No new read — already present on every column dict
            # returned by get_table_business_context.
            "semantic_type":    prof.get("semantic_type"),
        })
    return out


def _resolve_term(term: str, table_contexts: dict[str, dict], kind: str) -> dict:
    """
    Step 4/5/8. Score every eligible column across all candidate tables
    against `term`, rank, and only auto-select when the top candidate clears
    _AUTO_SELECT_MIN_CONFIDENCE with a clear margin over the runner-up.
    Never silently chooses a low-confidence option.

    Each column's score is the name-match score plus its own table's
    authority bonus (_score_table_authority — Milestone M-2), so a column in
    a well-governed, well-populated production table outranks the identically
    named column in a `_temp`/unapproved/low-evidence one.
    """
    predicate = _is_metric_column if kind == "measure" else _is_dimension_column
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        table_authority = _score_table_authority(table_fqn, ctx)
        candidates.extend(_score_candidates(term, table_fqn, ctx["columns"], predicate, table_authority))

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


def _resolve_count_all(term: str, table_contexts: dict[str, dict]) -> dict | None:
    """
    Bare row-count support ("How many clients?", "Number of students").

    A measure term that resolves to no metric COLUMN may still name a real
    TABLE directly (the term IS the entity, not an attribute of it). Scores
    `term` against each candidate table's own short name using the same
    _score_term_match/_AUTO_SELECT_MIN_CONFIDENCE/_AMBIGUITY_MARGIN rules
    already used for column matching — never invents a table that wasn't
    already a candidate in table_contexts.

    Score is the table-name match plus the table's own authority bonus
    (_score_table_authority — Milestone M-2: dictionary approval, domain/
    entity assignment, relationship coverage, row count, naming-convention
    penalties), so among several same-name-scoring "clients" tables the one
    with real evidence of being the authoritative source ranks first.

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
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        table_name = table_fqn.split(".")[-1]
        name_score = _score_term_match(term, table_name)
        authority = _score_table_authority(table_fqn, ctx)
        score = max(0.0, min(1.0, name_score + authority["bonus"]))
        candidates.append({
            "table_fqn": table_fqn, "column_name": None,
            "business_label": None, "score": score,
            "name_score": name_score,
            "authority_bonus": authority["bonus"],
            "ranking_reasons": authority["reasons"],
            "is_approved": bool(ctx.get("dictionary", {}).get("is_approved")) if ctx.get("dictionary") else False,
            "data_type": None,
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


def _resolve_entity_count(term: str, table_contexts: dict[str, dict], *, distinct_requested: bool) -> dict:
    """
    Resolve an entity-count term ("how many students", "number of clients")
    to a counted table + entity key. Reuses _resolve_count_all's existing
    table-name ranking unchanged (never invents a table); only key
    selection and the resulting shape are new. Mirrors _resolve_term's
    return shape (term/selected/candidates/warnings) so build_sql_plan's
    existing unresolved/ambiguous handling applies unchanged.
    """
    count_all = _resolve_count_all(term, table_contexts)
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

def _resolve_concept(term: str, table_contexts: dict[str, dict]) -> dict:
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        table_name = table_fqn.split(".")[-1]
        dictionary = ctx.get("dictionary") or {}
        domain = ctx.get("domain") or {}
        entity = ctx.get("entity") or {}
        relationships = ctx.get("relationships") or {}
        name_score = _score_term_match(term, table_name, dictionary.get("business_name"), dictionary.get("description"))
        authority = _score_table_authority(table_fqn, ctx)
        score = max(0.0, min(1.0, name_score + authority["bonus"]))
        candidates.append({
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
            "authority_bonus":  authority["bonus"],
            "ranking_reasons":  authority["reasons"],
        })

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


def _find_status_column(table_contexts: dict[str, dict], preferred_table: str | None) -> tuple[str, str] | None:
    """
    Locate a status column for status-value filtering. Primary signal:
    profiling semantic_type == 'STATUS' (core/profiling/classification/
    column_typer.py already classifies this). Fallback: column name token
    match on 'status'/'state' — the same naming-convention fallback pattern
    _is_metric_column already uses. Never invents a status column.
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
            if {"status", "state"} & set(_tokenize(col["column_name"])):
                return table_fqn, col["column_name"]
    return None


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


def _build_intent(question: str, measures: list[dict], dimensions: list[dict], query_intent: dict) -> dict:
    has_measure = any(m["selected"] for m in measures)
    has_dimension = any(d["selected"] for d in dimensions)
    if has_measure and has_dimension:
        qtype = "aggregate_by_dimension"
    elif has_measure:
        qtype = "aggregate_overall"
    elif has_dimension:
        qtype = "list_by_dimension"
    else:
        qtype = "unresolved"
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
# Public entry point
# ---------------------------------------------------------------------------

def plan_business_query(source_id: int, user_id: str, request: dict) -> dict | None:
    """
    Step 2. Compose a safe query plan from a structured business request.

    request = {"question": str, "concepts": [...], "measures": [...],
               "dimensions": [...], "filters": [...]}  (all keys optional
               except question; missing keys default to empty).

    Never generates SQL, never executes anything, never invents a table or
    column that wasn't returned by find_business_assets / get_table_business_context.

    Returns None when the source does not exist or is not owned by user_id.
    """
    conn = get_connection()
    try:
        if not _verify_source(conn, source_id, user_id):
            return None
    finally:
        conn.close()

    question        = (request.get("question") or "").strip()
    concepts        = list(dict.fromkeys(request.get("concepts") or []))
    measure_terms   = list(dict.fromkeys(request.get("measures") or []))
    dimension_terms = list(dict.fromkeys(request.get("dimensions") or []))
    filters         = request.get("filters") or []

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

    warnings: list[dict] = []
    if not all_terms:
        warnings.append({
            "type": "no_search_terms", "severity": "MEDIUM",
            "message": "No concepts, measures, or dimensions were provided to plan against.",
        })

    # Sprint 1 AI Brain: try domain-ranked retrieval first; an empty result
    # (weak/ambiguous domain, no search_metadata match, or any failure)
    # falls back to the original unbounded _collect_candidate_tables() path
    # unchanged, per the Sprint 1 contract.
    if all_terms:
        candidate_tables = _get_ai_candidate_tables(source_id, user_id, question, all_terms)
        if not candidate_tables:
            candidate_tables = _collect_candidate_tables(source_id, user_id, all_terms)
    else:
        candidate_tables = set()

    table_contexts: dict[str, dict] = {}
    for fqn in candidate_tables or set():
        ctx = get_table_business_context(source_id, user_id, fqn)
        if ctx:
            table_contexts[fqn] = ctx

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
    aggregation_target = query_intent.get("aggregation_target")
    if aggregation_target in ("entity_count", "distinct_entity_count"):
        measures = [
            _resolve_entity_count(
                t, table_contexts, distinct_requested=(aggregation_target == "distinct_entity_count"),
            )
            for t in measure_terms
        ]
    else:
        measures = [_resolve_term(t, table_contexts, "measure") for t in measure_terms]
    dimensions = [_resolve_term(t, table_contexts, "dimension") for t in dimension_terms]
    resolved_concepts = [_resolve_concept(t, table_contexts) for t in concepts]

    for entry in measures + dimensions:
        warnings.extend(entry["warnings"])

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

    join_plan = _plan_joins(source_id, user_id, primary_table, selected_tables)
    _apply_join_fanout_safety(measures, join_plan, warnings)
    warnings.extend(_collect_governance_warnings(table_contexts, measures, dimensions, join_plan))

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
    if query_intent["date_range"] and table_contexts:
        date_col = _find_date_column(table_contexts, primary_table)
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
    if query_intent["status_value"] and table_contexts:
        status_col = _find_status_column(table_contexts, primary_table)
        if status_col:
            fqn, col_name = status_col
            if col_name not in explicit_filter_columns:
                resolved_filters.append({
                    "column": col_name, "operator": "=", "value": query_intent["status_value"],
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

    # Ordering (Top N / Bottom N / Latest / Earliest) — resolve the target
    # column once here, in the semantic layer, exactly like measures/
    # dimensions/joins are already pre-resolved before SQL planning.
    resolved_order = None
    if query_intent["order"]:
        order = dict(query_intent["order"])
        if order.get("target") == "date":
            date_col = _find_date_column(table_contexts, primary_table)
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

    intent = _build_intent(question, measures, dimensions, {**query_intent, "order": resolved_order})
    confidence = _compute_confidence(measures, dimensions, join_plan, warnings)
    explanation = _build_explanation(measures, dimensions, join_plan, warnings)

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
    }
