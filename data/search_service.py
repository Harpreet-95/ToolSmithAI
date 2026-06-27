"""
Enterprise metadata search engine.

Searches across all stored metadata using keyword matching and multi-signal
relevance scoring.  No AI, no embeddings, no external dependencies.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from data.db import get_connection

# ---------------------------------------------------------------------------
# Relevance weights — one integer per searchable field
# ---------------------------------------------------------------------------
_W_TABLE_NAME      = 100
_W_BUSINESS_NAME   = 85
_W_DESCRIPTION     = 60
_W_SCHEMA_NAME     = 25
_W_SOURCE_NAME     = 20
_W_TABLE_CLASS     = 30
_W_DICT_DOMAIN     = 45
_W_ASSIGNED_DOMAIN = 40
_W_ASSIGNED_ENTITY = 40

_W_COLUMN_NAME     = 90
_W_BUSINESS_LABEL  = 85
_W_MEANING         = 60
_W_SEMANTIC_TYPE   = 50

_MAX_CANDIDATES = 2000  # cap DB rows before Python scoring


# ---------------------------------------------------------------------------
# Query tokenisation
# ---------------------------------------------------------------------------

def _tokenize(q: str) -> list[str]:
    """Lowercase, split on whitespace / underscore / dash, drop tokens < 2 chars."""
    tokens = re.split(r"[\s_\-/]+", q.lower().strip())
    return [t for t in tokens if len(t) >= 2]


# ---------------------------------------------------------------------------
# Synonym expansion
# ---------------------------------------------------------------------------

_SYNONYMS_PATH = Path(__file__).parent / "synonyms.json"


class _SynonymExpander:
    """Expands query tokens using a JSON-backed synonym dictionary.

    Each synonym group is a list of equivalent terms.  Any term in a group
    maps to the full group so that searching one term also scores against all
    others.  The dictionary lives in data/synonyms.json and is loaded once at
    module import time — add new groups there to extend coverage.
    """

    def __init__(self, groups: list[list[str]]) -> None:
        self._map: dict[str, frozenset[str]] = {}
        self._groups: list[frozenset[str]] = []
        for group in groups:
            # Normalise and drop terms shorter than 2 characters
            normalised = frozenset(t.lower() for t in group if len(t) >= 2)
            if len(normalised) < 2:
                continue  # single-term groups provide no expansion value
            self._groups.append(normalised)
            for term in normalised:
                self._map[term] = normalised

    def expand(self, token: str) -> frozenset[str]:
        """Return the full synonym set for *token*, including itself."""
        return self._map.get(token.lower(), frozenset({token.lower()}))

    def __len__(self) -> int:
        """Number of synonym groups loaded."""
        return len(self._groups)


def _load_synonym_expander() -> _SynonymExpander:
    """Load synonyms from disk; return an empty expander on any error."""
    try:
        raw = json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
        return _SynonymExpander(raw.get("groups", []))
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return _SynonymExpander([])


_SYNONYM_EXPANDER: _SynonymExpander = _load_synonym_expander()


def _expand_tokens(tokens: list[str]) -> list[str]:
    """Return *tokens* extended with synonyms from the loaded dictionary.

    Original tokens are preserved at the front in their original order.
    Synonym additions are appended alphabetically.  Deduplication ensures
    that if the query already contains a synonym term it appears only once.
    """
    seen: set[str] = set(tokens)
    result: list[str] = list(tokens)
    for tok in tokens:
        for syn in sorted(_SYNONYM_EXPANDER.expand(tok) - {tok}):
            if syn not in seen:
                seen.add(syn)
                result.append(syn)
    return result


# ---------------------------------------------------------------------------
# Per-field scoring
# ---------------------------------------------------------------------------

def _score_field(text: str | None, tokens: list[str], weight: int) -> int:
    """Additive score for one text field against all query tokens."""
    if not text:
        return 0
    t = text.lower()
    score = 0
    for tok in tokens:
        if tok == t:
            score += weight                              # exact full-field match
        elif re.search(rf"\b{re.escape(tok)}\b", t):
            score += int(weight * 0.85)                 # whole-word boundary
        elif tok in t:
            score += int(weight * 0.5)                  # substring
    return score


def _score_table_row(row: dict, tokens: list[str]) -> int:
    s = 0
    s += _score_field(row.get("table_name"),      tokens, _W_TABLE_NAME)
    s += _score_field(row.get("business_name"),   tokens, _W_BUSINESS_NAME)
    s += _score_field(row.get("description"),     tokens, _W_DESCRIPTION)
    s += _score_field(row.get("schema_name"),     tokens, _W_SCHEMA_NAME)
    s += _score_field(row.get("source_name"),     tokens, _W_SOURCE_NAME)
    s += _score_field(row.get("table_class"),     tokens, _W_TABLE_CLASS)
    s += _score_field(row.get("dict_domain"),     tokens, _W_DICT_DOMAIN)
    s += _score_field(row.get("assigned_domain"), tokens, _W_ASSIGNED_DOMAIN)
    s += _score_field(row.get("assigned_entity"), tokens, _W_ASSIGNED_ENTITY)
    return s


def _score_column_row(row: dict, tokens: list[str]) -> int:
    s = 0
    s += _score_field(row.get("column_name"),     tokens, _W_COLUMN_NAME)
    s += _score_field(row.get("business_label"),  tokens, _W_BUSINESS_LABEL)
    s += _score_field(row.get("meaning"),         tokens, _W_MEANING)
    s += _score_field(row.get("semantic_type"),   tokens, _W_SEMANTIC_TYPE)
    s += _score_field(row.get("table_name"),      tokens, _W_TABLE_NAME // 2)
    s += _score_field(row.get("source_name"),     tokens, _W_SOURCE_NAME)
    s += _score_field(row.get("assigned_domain"), tokens, _W_ASSIGNED_DOMAIN)
    s += _score_field(row.get("assigned_entity"), tokens, _W_ASSIGNED_ENTITY)
    return s


# ---------------------------------------------------------------------------
# Best-matching field for the result card "Matched because" label
# ---------------------------------------------------------------------------

_TABLE_FIELD_PRIORITY = [
    "table_name", "business_name", "description",
    "dict_domain", "assigned_domain", "assigned_entity",
    "table_class", "schema_name", "source_name",
]
_COLUMN_FIELD_PRIORITY = [
    "column_name", "business_label", "meaning",
    "semantic_type", "table_name", "source_name",
    "assigned_domain", "assigned_entity",
]

def _best_match(row: dict, tokens: list[str], priority: list[str]) -> tuple[str, str]:
    """Return (field_name, field_value) for the first field that matches any token."""
    for field in priority:
        val = row.get(field)
        if val:
            vl = val.lower()
            for tok in tokens:
                if tok in vl:
                    return field, val
    return "unknown", ""


# ---------------------------------------------------------------------------
# Dynamic SQL helpers
# ---------------------------------------------------------------------------

def _where_block(fields: list[str], n_tokens: int) -> str:
    """
    Build a WHERE clause fragment:
      (field1 LIKE ? OR field2 LIKE ? OR ...) OR (field1 LIKE ? OR ...) ...
    One group per token, OR-joined across tokens.
    """
    per_token = " OR ".join(f"{f} LIKE ?" for f in fields)
    groups = [f"({per_token})" for _ in range(n_tokens)]
    return " OR ".join(groups)


def _like_params(tokens: list[str], n_fields: int) -> list[str]:
    """Flat list of LIKE params: for each token, repeat '%token%' n_fields times."""
    params: list[str] = []
    for tok in tokens:
        like = f"%{tok}%"
        params.extend([like] * n_fields)
    return params


# ---------------------------------------------------------------------------
# Base SQL for table-level candidates
# ---------------------------------------------------------------------------

_TABLE_SEARCH_FIELDS = [
    "ptp.table_name",
    "ptp.schema_name",
    "dsc.display_name",
    "ddt.business_name",
    "ddt.description",
    "ddt.domain",
    "ptp.table_class",
    "da.domain",
    "ea.entity",
]

_TABLE_BASE_SQL = """
    SELECT
        ptp.source_id,
        dsc.display_name          AS source_name,
        ptp.schema_name,
        ptp.table_name,
        ptp.table_fqn,
        ptp.table_type,
        ptp.table_class,
        ptp.pii_column_count,
        ddt.business_name,
        ddt.description,
        ddt.domain                AS dict_domain,
        ddt.is_approved           AS dict_approved,
        da.domain                 AS assigned_domain,
        da.confidence             AS domain_confidence,
        ea.entity                 AS assigned_entity,
        ea.confidence             AS entity_confidence
    FROM profiling_table_profiles ptp
    JOIN data_source_connections dsc
        ON dsc.id = ptp.source_id
    LEFT JOIN data_dictionary_tables ddt
        ON ddt.source_id = ptp.source_id
        AND ddt.table_fqn = ptp.table_fqn
    LEFT JOIN domain_assignments da
        ON da.source_id = ptp.source_id
        AND da.table_fqn = ptp.table_fqn
    LEFT JOIN entity_assignments ea
        ON ea.source_id = ptp.source_id
        AND ea.table_fqn = ptp.table_fqn
    WHERE ({where})
"""


# ---------------------------------------------------------------------------
# Base SQL for column-level candidates
# ---------------------------------------------------------------------------

_COLUMN_SEARCH_FIELDS = [
    "pcp.column_name",
    "ddc.business_label",
    "ddc.meaning",
    "pcp.semantic_type",
    "dsc.display_name",
    "ptp.table_name",
    "da.domain",
    "ea.entity",
]

_COLUMN_BASE_SQL = """
    SELECT
        pcp.source_id,
        dsc.display_name          AS source_name,
        ptp.schema_name,
        ptp.table_name,
        pcp.table_fqn,
        pcp.column_name,
        pcp.semantic_type,
        pcp.pii_confirmed,
        ddc.business_label,
        ddc.meaning,
        ddc.pii_risk,
        ddc.is_approved           AS dict_approved,
        da.domain                 AS assigned_domain,
        ea.entity                 AS assigned_entity,
        ptp.table_class
    FROM profiling_column_profiles pcp
    JOIN data_source_connections dsc
        ON dsc.id = pcp.source_id
    LEFT JOIN profiling_table_profiles ptp
        ON ptp.source_id = pcp.source_id
        AND ptp.table_fqn = pcp.table_fqn
    LEFT JOIN data_dictionary_columns ddc
        ON ddc.source_id = pcp.source_id
        AND ddc.table_fqn = pcp.table_fqn
        AND ddc.column_name = pcp.column_name
    LEFT JOIN domain_assignments da
        ON da.source_id = pcp.source_id
        AND da.table_fqn = pcp.table_fqn
    LEFT JOIN entity_assignments ea
        ON ea.source_id = pcp.source_id
        AND ea.table_fqn = pcp.table_fqn
    WHERE ({where})
"""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_metadata(
    q: str,
    limit: int = 20,
    offset: int = 0,
    source_id: Optional[int] = None,
    asset_type: Optional[str] = None,
) -> dict:
    """
    Keyword search across all stored metadata.

    Parameters
    ----------
    q           : search query string
    limit       : max results per page (1–100)
    offset      : pagination offset
    source_id   : restrict to a single data source
    asset_type  : one of 'table', 'column', 'dictionary', 'domain', 'entity';
                  None means search all

    Returns
    -------
    {results, total, query, tokens}
    """
    q = (q or "").strip()
    if not q:
        return {"results": [], "total": 0, "query": q, "tokens": []}

    tokens = _tokenize(q)
    if not tokens:
        return {"results": [], "total": 0, "query": q, "tokens": []}

    # Expand query tokens with synonyms for SQL candidate fetch and scoring.
    # The original tokens are preserved in the response for backward compatibility.
    expanded_tokens = _expand_tokens(tokens)

    include_tables  = asset_type in (None, "table", "dictionary", "domain", "entity")
    include_columns = asset_type in (None, "column", "dictionary")

    conn = get_connection()
    cursor = conn.cursor()
    results: list[dict] = []

    try:
        # ── Table-level search ────────────────────────────────────────────────
        if include_tables:
            where = _where_block(_TABLE_SEARCH_FIELDS, len(expanded_tokens))
            sql   = _TABLE_BASE_SQL.format(where=where)
            params: list = _like_params(expanded_tokens, len(_TABLE_SEARCH_FIELDS))

            if source_id is not None:
                sql += " AND ptp.source_id = ?"
                params.append(source_id)
            if asset_type == "domain":
                sql += " AND (da.domain IS NOT NULL OR ddt.domain IS NOT NULL)"
            elif asset_type == "entity":
                sql += " AND ea.entity IS NOT NULL"
            elif asset_type == "dictionary":
                sql += " AND (ddt.business_name IS NOT NULL OR ddt.description IS NOT NULL)"

            sql += f" LIMIT {_MAX_CANDIDATES}"
            rows = cursor.execute(sql, params).fetchall()

            for row in rows:
                d = dict(row)
                score = _score_table_row(d, expanded_tokens)
                if score <= 0:
                    continue
                matched_f, matched_t = _best_match(d, expanded_tokens, _TABLE_FIELD_PRIORITY)
                domain = d.get("assigned_domain") or d.get("dict_domain") or ""
                entity = d.get("assigned_entity") or ""
                pii = bool(d.get("pii_column_count") and d["pii_column_count"] > 0)
                results.append({
                    "asset_type":        "table",
                    "display_name":      d.get("business_name") or d.get("table_name") or "",
                    "qualified_name":    d.get("table_fqn") or "",
                    "source_id":         d.get("source_id"),
                    "source_name":       d.get("source_name") or "",
                    "schema_name":       d.get("schema_name") or "",
                    "table_name":        d.get("table_name") or "",
                    "column_name":       None,
                    "matched_field":     matched_f,
                    "matched_text":      matched_t,
                    "relevance_score":   score,
                    "short_description": (d.get("description") or "")[:200],
                    "domain":            domain,
                    "entity":            entity,
                    "dictionary_status": (
                        "approved"  if d.get("dict_approved")
                        else "generated" if d.get("business_name")
                        else "none"
                    ),
                    "pii_indicator":     pii,
                    "semantic_type":     d.get("table_class") or "",
                    "table_type":        d.get("table_type") or "TABLE",
                    "nav_target": {
                        "view":      "data-sources",
                        "source_id": d.get("source_id"),
                        "table_fqn": d.get("table_fqn"),
                    },
                })

        # ── Column-level search ───────────────────────────────────────────────
        if include_columns:
            where = _where_block(_COLUMN_SEARCH_FIELDS, len(expanded_tokens))
            sql   = _COLUMN_BASE_SQL.format(where=where)
            params = _like_params(expanded_tokens, len(_COLUMN_SEARCH_FIELDS))

            if source_id is not None:
                sql += " AND pcp.source_id = ?"
                params.append(source_id)
            if asset_type == "dictionary":
                sql += " AND (ddc.business_label IS NOT NULL OR ddc.meaning IS NOT NULL)"

            sql += f" LIMIT {_MAX_CANDIDATES}"
            rows = cursor.execute(sql, params).fetchall()

            for row in rows:
                d = dict(row)
                score = _score_column_row(d, expanded_tokens)
                if score <= 0:
                    continue
                matched_f, matched_t = _best_match(d, expanded_tokens, _COLUMN_FIELD_PRIORITY)
                domain = d.get("assigned_domain") or ""
                entity = d.get("assigned_entity") or ""
                pii    = bool(d.get("pii_confirmed") or d.get("pii_risk"))
                results.append({
                    "asset_type":        "column",
                    "display_name":      d.get("business_label") or d.get("column_name") or "",
                    "qualified_name":    f"{d.get('table_fqn', '')}.{d.get('column_name', '')}",
                    "source_id":         d.get("source_id"),
                    "source_name":       d.get("source_name") or "",
                    "schema_name":       d.get("schema_name") or "",
                    "table_name":        d.get("table_name") or "",
                    "column_name":       d.get("column_name") or "",
                    "matched_field":     matched_f,
                    "matched_text":      matched_t,
                    "relevance_score":   score,
                    "short_description": (d.get("meaning") or "")[:200],
                    "domain":            domain,
                    "entity":            entity,
                    "dictionary_status": (
                        "approved"  if d.get("dict_approved")
                        else "generated" if d.get("business_label")
                        else "none"
                    ),
                    "pii_indicator":     pii,
                    "semantic_type":     d.get("semantic_type") or "",
                    "table_type":        None,
                    "nav_target": {
                        "view":        "data-sources",
                        "source_id":   d.get("source_id"),
                        "table_fqn":   d.get("table_fqn"),
                        "column_name": d.get("column_name"),
                    },
                })

    finally:
        conn.close()

    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    total  = len(results)
    page   = results[offset : offset + limit]

    return {
        "results": page,
        "total":   total,
        "query":   q,
        "tokens":  tokens,      # original tokens only — expanded set is internal
    }
