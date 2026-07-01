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

from data.db import get_connection
from data.knowledge_graph_service import find_business_assets
from data.business_knowledge_service import get_table_business_context
from data.semantic_layer_service import analyze_join_quality, recommend_best_join_path
from core.dictionary.rule_classifier import _METRIC_TOKENS, _tokenize

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


def _verify_source(conn, source_id: int, user_id: str) -> bool:
    row = conn.execute(
        "SELECT id FROM data_source_connections WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Term matching — deterministic token-overlap scoring, no NLP library, no LLM
# ---------------------------------------------------------------------------

def _score_term_match(term: str, *texts: str | None) -> float:
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


# ---------------------------------------------------------------------------
# Step 3 — Table/column discovery (composes find_business_assets)
# ---------------------------------------------------------------------------

def _collect_candidate_tables(source_id: int, user_id: str, terms: list[str]) -> set[str] | None:
    """
    Run find_business_assets once per term, union the candidate table_fqns.
    Returns None on an ownership failure (propagated to the caller's own
    upfront check — this should not happen in practice since
    plan_business_query already verifies ownership first).
    """
    candidate_tables: set[str] = set()
    for term in terms:
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


def _score_candidates(term: str, table_fqn: str, columns: list[dict], predicate) -> list[dict]:
    out = []
    for col in columns:
        if not predicate(col):
            continue
        dic = col.get("dictionary") or {}
        score = _score_term_match(term, col["column_name"], dic.get("business_label"), dic.get("meaning"))
        out.append({
            "table_fqn":      table_fqn,
            "column_name":    col["column_name"],
            "business_label": dic.get("business_label"),
            "score":          score,
            "is_approved":    bool(dic.get("is_approved")) if dic else False,
            "data_type":      (col.get("schema") or {}).get("data_type"),
        })
    return out


def _resolve_term(term: str, table_contexts: dict[str, dict], kind: str) -> dict:
    """
    Step 4/5/8. Score every eligible column across all candidate tables
    against `term`, rank, and only auto-select when the top candidate clears
    _AUTO_SELECT_MIN_CONFIDENCE with a clear margin over the runner-up.
    Never silently chooses a low-confidence option.
    """
    predicate = _is_metric_column if kind == "measure" else _is_dimension_column
    candidates: list[dict] = []
    for table_fqn, ctx in table_contexts.items():
        candidates.extend(_score_candidates(term, table_fqn, ctx["columns"], predicate))

    candidates.sort(key=lambda c: -c["score"])

    selected = None
    warnings: list[dict] = []
    if not candidates:
        warnings.append({
            "type": f"missing_{kind}", "severity": "MEDIUM",
            "message": f"No {kind} candidate found for '{term}'.",
        })
    else:
        top = candidates[0]
        runner_up_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
        if top["score"] >= _AUTO_SELECT_MIN_CONFIDENCE and (top["score"] - runner_up_score) >= _AMBIGUITY_MARGIN:
            selected = top
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

def _infer_aggregation(selected_measure: dict | None) -> str | None:
    if not selected_measure:
        return None
    name = f"{selected_measure.get('column_name') or ''} {selected_measure.get('business_label') or ''}"
    toks = set(_tokenize(name))
    for tok, agg in _AGGREGATION_HINTS.items():
        if tok in toks:
            return agg
    return "SUM"


def _build_intent(question: str, measures: list[dict], dimensions: list[dict]) -> dict:
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
        "aggregation":  _infer_aggregation(primary_measure),
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

    all_terms = list(dict.fromkeys(concepts + measure_terms + dimension_terms))

    warnings: list[dict] = []
    if not all_terms:
        warnings.append({
            "type": "no_search_terms", "severity": "MEDIUM",
            "message": "No concepts, measures, or dimensions were provided to plan against.",
        })

    candidate_tables = _collect_candidate_tables(source_id, user_id, all_terms) if all_terms else set()

    table_contexts: dict[str, dict] = {}
    for fqn in candidate_tables or set():
        ctx = get_table_business_context(source_id, user_id, fqn)
        if ctx:
            table_contexts[fqn] = ctx

    measures = [_resolve_term(t, table_contexts, "measure") for t in measure_terms]
    dimensions = [_resolve_term(t, table_contexts, "dimension") for t in dimension_terms]
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

    intent = _build_intent(question, measures, dimensions)
    confidence = _compute_confidence(measures, dimensions, join_plan, warnings)
    explanation = _build_explanation(measures, dimensions, join_plan, warnings)

    return {
        "source_id":   source_id,
        "intent":      intent,
        "tables":      sorted(table_contexts.keys()),
        "columns":     {fqn: [c["column_name"] for c in ctx["columns"]] for fqn, ctx in table_contexts.items()},
        "measures":    measures,
        "dimensions":  dimensions,
        "filters":     resolved_filters,
        "join_plan":   join_plan,
        "warnings":    warnings,
        "confidence":  confidence,
        "explanation": explanation,
    }
