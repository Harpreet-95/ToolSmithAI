from __future__ import annotations

from core.dictionary.rule_classifier import _tokenize
from core.semantic.execution_plan import ConceptMatch, ConceptStatus

# Small, deterministic stopword set for splitting a raw business question into
# candidate concept/measure/dimension terms. Deliberately mirrors the same
# "split on 'by', drop stopwords" heuristic already used by the existing
# api/v1/routes.py::_extract_query_terms for the /execute-query route — that
# function lives in the API routing layer and core/ must not import from
# api/, so this is a small, faithful reimplementation rather than a
# cross-layer import. No AI/ML — deterministic token heuristic only.
_STOPWORDS = frozenset({
    "show", "me", "the", "a", "an", "of", "for", "how", "many", "what",
    "is", "are", "from", "in", "on", "at", "with", "and", "or", "give",
    "get", "list", "display", "find", "tell", "which", "per", "each", "all",
    "their", "its", "using", "across", "among", "between",
})

# A term counts as "ambiguous" when the top two distinct-table search results
# score within this margin of each other on search_metadata's relevance scale.
_AMBIGUITY_MARGIN = 15.0


def extract_terms(question: str) -> tuple[list[str], list[str], list[str]]:
    """Split a NL question into (concepts, measure_terms, dimension_terms).

    Words after "by" are dimension hints; all other content words (minus
    stopwords) become both concepts and measure candidates.
    """
    words = [w for w in _tokenize(question or "") if w]
    try:
        by_idx = words.index("by")
        after = [w for w in words[by_idx + 1:] if w not in _STOPWORDS]
        before = [w for w in words[:by_idx] if w not in _STOPWORDS]
    except ValueError:
        after = []
        before = [w for w in words if w not in _STOPWORDS]

    concepts = list(dict.fromkeys(before + after))
    measures = list(dict.fromkeys(before))
    dimensions = list(dict.fromkeys(after))
    return concepts, measures, dimensions


def _table_ref(result: dict) -> str:
    schema = result.get("schema_name") or ""
    table = result.get("table_name") or ""
    return f"{schema}.{table}" if schema or table else ""


def resolve_concepts(source_id: int, user_id: str, terms: list[str]) -> list[ConceptMatch]:
    """
    For each term, search existing metadata (dictionary/domain/entity/schema)
    for what it actually matches on this source. Never invents a match —
    every matched table/column/domain/entity comes straight from
    data.search_service.search_metadata's own results.
    """
    from data.search_service import search_metadata

    matches: list[ConceptMatch] = []
    for term in terms:
        try:
            result = search_metadata(term, source_id=source_id, limit=10)
        except Exception:  # noqa: BLE001
            result = {"results": []}

        results = result.get("results") or []
        if not results:
            matches.append(ConceptMatch(term=term, status=ConceptStatus.UNKNOWN))
            continue

        tables = list(dict.fromkeys(_table_ref(r) for r in results if _table_ref(r)))
        columns = [
            {"table_fqn": _table_ref(r), "column_name": r["column_name"]}
            for r in results if r.get("asset_type") == "column" and r.get("column_name")
        ]
        domains = sorted({r["domain"] for r in results if r.get("domain")})
        entities = sorted({r["entity"] for r in results if r.get("entity")})

        top_score = results[0]["relevance_score"]
        distinct_table_scores = list(dict.fromkeys(
            (r["relevance_score"], _table_ref(r)) for r in results
        ))
        distinct_tables_seen = {t for _, t in distinct_table_scores}

        status = ConceptStatus.RESOLVED
        if len(distinct_tables_seen) >= 2:
            scores_by_table: dict[str, float] = {}
            for score, table in distinct_table_scores:
                scores_by_table.setdefault(table, score)
            ranked = sorted(scores_by_table.values(), reverse=True)
            if len(ranked) >= 2 and (ranked[0] - ranked[1]) < _AMBIGUITY_MARGIN:
                status = ConceptStatus.AMBIGUOUS

        matches.append(ConceptMatch(
            term=term,
            status=status,
            matched_tables=tables,
            matched_columns=columns,
            matched_domains=domains,
            matched_entities=entities,
            confidence=round(min(1.0, top_score / 100.0), 4),
        ))

    return matches
