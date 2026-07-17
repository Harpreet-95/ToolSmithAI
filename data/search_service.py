"""
Enterprise metadata search engine.

Searches across all stored metadata using keyword matching and multi-signal
relevance scoring.  No AI, no embeddings, no external dependencies.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from data.db import get_connection
# Reused only for its camelCase-aware tokenizer (same one
# query_planning_service._score_table_authority uses for its naming
# penalty) — aliased to avoid colliding with this module's own _tokenize(),
# which splits query text, not table names. No circular import: this module
# has no dependency back on search_service/query_planning_service.
from core.dictionary.rule_classifier import _tokenize as _camel_tokenize

logger = logging.getLogger(__name__)

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

_PHRASE_BONUS               = 150  # awarded when the full query phrase appears verbatim in a key text field
_MULTI_FIELD_BONUS_PER_FIELD = 25  # awarded per additional distinct field hit beyond the first


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
# Conservative singular/plural normalization (Sprint 1.2, Fix 1)
#
# Query terms are near-always plural ("students", "courses", "classes") while
# table names are near-always singular (ADF_Student, ADF_Course, ADF_Class).
# This is not a stemmer — it only reverses the three regular English plural
# endings (-s, -es, -ies) and is deliberately tried only after every literal
# form of the token has already failed to match, so an exact/substring hit on
# the literal query term always outranks a normalized-form hit.
# ---------------------------------------------------------------------------

# Endings where a trailing "s" is very unlikely to be a regular plural
# (status, campus, analysis, ...) — left untouched rather than singularized.
_NON_PLURAL_S_ENDINGS = ("ss", "us", "is", "os")


def _singularize(word: str) -> str:
    """Best-effort singular form for plural-matching only. Returns *word*
    unchanged when no safe rule applies (short words, or endings excluded
    above) — never raises, never guesses aggressively."""
    if len(word) < 4:
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"                          # companies -> company
    # "ss"/"x"/"z"/"ch"/"sh" endings pluralize with -es (class -> classes).
    # A bare "s" here is deliberately excluded: silent-e words like
    # "course" also end in "s" once stripped of their own trailing "es"
    # (courses -> cours), which would wrongly fire this branch instead of
    # the plain -s rule below (courses -> course).
    if word.endswith("es") and word[:-2].endswith(("ss", "x", "z", "ch", "sh")):
        return word[:-2]                                 # classes -> class
    if word.endswith("s") and not word.endswith(_NON_PLURAL_S_ENDINGS):
        return word[:-1]                                 # courses -> course
    return word


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
            continue
        if re.search(rf"\b{re.escape(tok)}\b", t):
            score += int(weight * 0.85)                 # whole-word boundary
            continue
        if tok in t:
            score += int(weight * 0.5)                  # substring
            continue

        # Singular/plural normalization — only reached when the literal
        # token failed every direct check above.
        singular = _singularize(tok)
        if singular != tok:
            if re.search(rf"\b{re.escape(singular)}\b", t):
                score += int(weight * 0.75)             # normalized whole-word
            elif singular in t:
                score += int(weight * 0.45)             # normalized substring
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
# Detailed scoring — per-field labels, phrase check, multi-field bonus
# ---------------------------------------------------------------------------

_TABLE_FIELD_LABELS: dict[str, str] = {
    "table_name":      "Table name",
    "business_name":   "Business name",
    "description":     "Dictionary definition",
    "schema_name":     "Schema name",
    "source_name":     "Source name",
    "table_class":     "Table classification",
    "dict_domain":     "Dictionary domain",
    "assigned_domain": "Domain assignment",
    "assigned_entity": "Entity assignment",
}
_COLUMN_FIELD_LABELS: dict[str, str] = {
    "column_name":     "Column name",
    "business_label":  "Business name",
    "meaning":         "Dictionary definition",
    "semantic_type":   "Semantic type",
    "table_name":      "Table name",
    "source_name":     "Source name",
    "assigned_domain": "Domain assignment",
    "assigned_entity": "Entity assignment",
}

# High-signal text fields searched for verbatim phrase presence
_TABLE_PHRASE_FIELDS  = ["table_name", "business_name", "description"]
_COLUMN_PHRASE_FIELDS = ["column_name", "business_label", "meaning"]


def _score_table_detailed(row: dict, tokens: list[str]) -> tuple[int, list[str]]:
    """Score a table row; return (base_score, match_reasons).

    Arithmetic is identical to _score_table_row.  Additionally collects the
    human-readable label for every field that contributed a non-zero score.
    """
    score = 0
    reasons: list[str] = []
    for field, weight in (
        ("table_name",      _W_TABLE_NAME),
        ("business_name",   _W_BUSINESS_NAME),
        ("description",     _W_DESCRIPTION),
        ("schema_name",     _W_SCHEMA_NAME),
        ("source_name",     _W_SOURCE_NAME),
        ("table_class",     _W_TABLE_CLASS),
        ("dict_domain",     _W_DICT_DOMAIN),
        ("assigned_domain", _W_ASSIGNED_DOMAIN),
        ("assigned_entity", _W_ASSIGNED_ENTITY),
    ):
        fs = _score_field(row.get(field), tokens, weight)
        if fs > 0:
            score += fs
            reasons.append(_TABLE_FIELD_LABELS[field])
    return score, reasons


# ---------------------------------------------------------------------------
# Non-authoritative naming penalty (Sprint 1.2, Fix 2)
#
# Same concept as query_planning_service._score_table_authority's naming
# penalty (_NEGATIVE_NAME_TOKENS / _NEGATIVE_NAME_SUBSTRINGS / _DATED_COPY_RE)
# — mirrored here rather than imported, since importing query_planning_service
# into search_service would be circular (query_planning_service ->
# semantic_retrieval_service -> search_service). Values match that module's
# set, extended with "test" and "bkup": both observed on real CCPP tables
# (ADF_Homework_test, Agent_TestCases) but absent from the original list.
# Calibrated to THIS module's ~20-900 point integer score scale, not
# query_planning_service's 0-1 bonus scale — same per-hit/cap structure,
# different units. Penalizes only; never excludes a table outright.
# ---------------------------------------------------------------------------
_NEGATIVE_NAME_TOKENS = {
    "temp", "tmp", "backup", "bkup", "old", "archive", "history", "log",
    "msgs", "import", "staging", "snapshot", "copy", "generated", "test",
}
_NEGATIVE_NAME_SUBSTRINGS = ("zoominfo", "clickup")
_DATED_COPY_RE = re.compile(r"(19|20)\d{2}|\d{1,2}[A-Za-z]{3,9}\d{2,4}")

_NAMING_PENALTY_PER_HIT = 40
_NAMING_PENALTY_CAP     = 120


def _naming_penalty(table_name: str) -> int:
    """Points to subtract from a table's relevance score for temp/backup/
    test/history/dated-copy naming patterns. Never removes a result — the
    caller still includes it, just ranked lower than an equivalent clean
    table."""
    if not table_name:
        return 0
    name_lower = table_name.lower()
    hits = set(_camel_tokenize(table_name)) & _NEGATIVE_NAME_TOKENS
    hits |= {kw for kw in _NEGATIVE_NAME_SUBSTRINGS if kw in name_lower}
    if _DATED_COPY_RE.search(table_name):
        hits.add("dated copy")
    if not hits:
        return 0
    return min(_NAMING_PENALTY_CAP, _NAMING_PENALTY_PER_HIT * len(hits))


def _score_column_detailed(row: dict, tokens: list[str]) -> tuple[int, list[str]]:
    """Score a column row; return (base_score, match_reasons)."""
    score = 0
    reasons: list[str] = []
    for field, weight in (
        ("column_name",     _W_COLUMN_NAME),
        ("business_label",  _W_BUSINESS_LABEL),
        ("meaning",         _W_MEANING),
        ("semantic_type",   _W_SEMANTIC_TYPE),
        ("table_name",      _W_TABLE_NAME // 2),
        ("source_name",     _W_SOURCE_NAME),
        ("assigned_domain", _W_ASSIGNED_DOMAIN),
        ("assigned_entity", _W_ASSIGNED_ENTITY),
    ):
        fs = _score_field(row.get(field), tokens, weight)
        if fs > 0:
            score += fs
            reasons.append(_COLUMN_FIELD_LABELS[field])
    return score, reasons


def _check_phrase(row: dict, phrase_fields: list[str], query_phrase: str) -> bool:
    """Return True if the lowercased query phrase appears verbatim in any phrase field."""
    phrase = query_phrase.lower().strip()
    if not phrase:
        return False
    for field in phrase_fields:
        val = row.get(field)
        if val and phrase in val.lower():
            return True
    return False


def _multi_field_bonus(token_matched_field_count: int) -> int:
    """Bonus for matching more than one distinct field with query tokens."""
    return max(0, token_matched_field_count - 1) * _MULTI_FIELD_BONUS_PER_FIELD


# ---------------------------------------------------------------------------
# Navigation target builders — produce structured nav objects from result rows
# ---------------------------------------------------------------------------

def _table_nav_target(d: dict, asset_type: Optional[str]) -> dict:
    """Return the correct nav_target for a table-level result.

    When the query was filtered by dictionary / domain / entity, the navigation
    target points at that specific tab rather than the generic schema tab.
    All IDs come from real DB rows — nothing is hardcoded.
    """
    if asset_type == "dictionary" and d.get("dict_id"):
        return {
            "type":          "dictionary",
            "source_id":     d.get("source_id"),
            "dictionary_id": d.get("dict_id"),
            "tab":           "dictionary",
        }
    if asset_type == "domain" and d.get("domain_id"):
        return {
            "type":      "domain",
            "source_id": d.get("source_id"),
            "domain_id": d.get("domain_id"),
            "tab":       "domains",
        }
    if asset_type == "entity" and d.get("entity_id"):
        return {
            "type":      "entity",
            "source_id": d.get("source_id"),
            "entity_id": d.get("entity_id"),
            "tab":       "entities",
        }
    return {
        "type":      "table",
        "source_id": d.get("source_id"),
        "schema":    d.get("schema_name") or "",
        "table_fqn": d.get("table_fqn") or "",
        "tab":       "schema",
    }


def _column_nav_target(d: dict, asset_type: Optional[str]) -> dict:
    """Return the correct nav_target for a column-level result."""
    if asset_type == "dictionary" and d.get("dict_col_id"):
        return {
            "type":          "dictionary",
            "source_id":     d.get("source_id"),
            "dictionary_id": d.get("dict_col_id"),
            "tab":           "dictionary",
        }
    return {
        "type":        "column",
        "source_id":   d.get("source_id"),
        "schema":      d.get("schema_name") or "",
        "table_fqn":   d.get("table_fqn") or "",
        "column_name": d.get("column_name") or "",
        "tab":         "profile",
    }


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
        ptp.profiling_status,
        ptp.updated_at            AS profiled_at,
        ddt.id                    AS dict_id,
        ddt.business_name,
        ddt.description,
        ddt.domain                AS dict_domain,
        ddt.is_approved           AS dict_approved,
        da.id                     AS domain_id,
        da.domain                 AS assigned_domain,
        da.confidence             AS domain_confidence,
        ea.id                     AS entity_id,
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
        ddc.id                    AS dict_col_id,
        ddc.business_label,
        ddc.meaning,
        ddc.pii_risk,
        ddc.is_approved           AS dict_approved,
        da.domain                 AS assigned_domain,
        da.confidence             AS domain_confidence,
        ea.entity                 AS assigned_entity,
        ea.confidence             AS entity_confidence,
        ptp.table_class,
        pcp.profiling_status,
        pcp.updated_at            AS profiled_at
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
# Discovery-anchored table rows — makes tables searchable immediately after
# schema discovery, before profiling or dictionary generation have run.
# Mirrors the schemas->tables JSON navigation
# relationship_service._parse_fks_from_snapshot_json already uses; profiling/
# dictionary/domain/entity data is looked up per table_fqn and left absent
# (None/0) rather than required. Only used for the single-source_id search
# path — cross-source search (source_id=None) keeps the original
# profiling_table_profiles-anchored query untouched.
# ---------------------------------------------------------------------------

def _discovered_tables(conn, source_id: int) -> list[dict]:
    """Every table/view in the source's latest schema_snapshots row.
    Returns [] on no snapshot, a missing schema_snapshots table (older
    fixtures/callers that never populated it), or a parse failure — never
    raises. An empty result signals the caller to fall back to the legacy
    profiling-anchored query rather than to report zero tables."""
    try:
        row = conn.execute(
            "SELECT snapshot_json FROM schema_snapshots WHERE source_id = ? "
            "ORDER BY snapshot_version DESC LIMIT 1",
            (source_id,),
        ).fetchone()
    except Exception:
        logger.warning("search_service: schema_snapshots unavailable for source_id=%s", source_id)
        return []
    if not row or not row["snapshot_json"]:
        return []
    try:
        data = json.loads(row["snapshot_json"])
    except (json.JSONDecodeError, TypeError):
        logger.warning("search_service: failed to parse snapshot_json for source_id=%s", source_id)
        return []

    tables: list[dict] = []
    for schema in data.get("schemas") or []:
        schema_name = schema.get("schema_name") or ""
        for table in schema.get("tables") or []:
            table_name = table.get("table_name") or ""
            table_fqn  = table.get("table_fqn") or f"{schema_name}.{table_name}"
            tables.append({
                "schema_name": schema_name,
                "table_name":  table_name,
                "table_fqn":   table_fqn,
                "table_type":  table.get("table_type") or "TABLE",
            })
    return tables


def _table_enrichment(conn, source_id: int) -> dict[str, dict]:
    """Per-table_fqn profiling/dictionary/domain/entity data for the source,
    where it exists. A table_fqn with no matching row here simply has none
    of these keys — callers must treat every key as optional."""
    enrichment: dict[str, dict] = {}

    for r in conn.execute(
        "SELECT table_fqn, table_class, pii_column_count, profiling_status, updated_at "
        "FROM profiling_table_profiles WHERE source_id = ?", (source_id,),
    ).fetchall():
        d = dict(r)
        enrichment.setdefault(d["table_fqn"], {}).update({
            "table_class":      d["table_class"],
            "pii_column_count": d["pii_column_count"],
            "profiling_status": d["profiling_status"],
            "profiled_at":      d["updated_at"],
        })

    for r in conn.execute(
        "SELECT table_fqn, id, business_name, description, domain, is_approved "
        "FROM data_dictionary_tables WHERE source_id = ?", (source_id,),
    ).fetchall():
        d = dict(r)
        enrichment.setdefault(d["table_fqn"], {}).update({
            "dict_id":       d["id"],
            "business_name": d["business_name"],
            "description":   d["description"],
            "dict_domain":   d["domain"],
            "dict_approved": d["is_approved"],
        })

    for r in conn.execute(
        "SELECT table_fqn, id, domain, confidence FROM domain_assignments WHERE source_id = ?",
        (source_id,),
    ).fetchall():
        d = dict(r)
        enrichment.setdefault(d["table_fqn"], {}).update({
            "domain_id":         d["id"],
            "assigned_domain":   d["domain"],
            "domain_confidence": d["confidence"],
        })

    for r in conn.execute(
        "SELECT table_fqn, id, entity, confidence FROM entity_assignments WHERE source_id = ?",
        (source_id,),
    ).fetchall():
        d = dict(r)
        enrichment.setdefault(d["table_fqn"], {}).update({
            "entity_id":         d["id"],
            "assigned_entity":   d["entity"],
            "entity_confidence": d["confidence"],
        })

    return enrichment


def _discovery_anchored_table_rows(conn, source_id: int, discovered: list[dict]) -> list[dict]:
    """Build the same row shape _TABLE_BASE_SQL produces, anchored on every
    discovered table for the source rather than only profiled ones. Missing
    profiling/dictionary/domain/entity data defaults to None/0, never
    required."""
    enrichment = _table_enrichment(conn, source_id)
    src_row = conn.execute(
        "SELECT display_name FROM data_source_connections WHERE id = ?", (source_id,),
    ).fetchone()
    source_name = src_row["display_name"] if src_row else ""

    rows: list[dict] = []
    for t in discovered:
        e = enrichment.get(t["table_fqn"], {})
        rows.append({
            "source_id":         source_id,
            "source_name":       source_name,
            "schema_name":       t["schema_name"],
            "table_name":        t["table_name"],
            "table_fqn":         t["table_fqn"],
            "table_type":        t["table_type"],
            "table_class":       e.get("table_class"),
            "pii_column_count":  e.get("pii_column_count") or 0,
            "profiling_status":  e.get("profiling_status"),
            "profiled_at":       e.get("profiled_at") or "",
            "dict_id":           e.get("dict_id"),
            "business_name":     e.get("business_name"),
            "description":       e.get("description"),
            "dict_domain":       e.get("dict_domain"),
            "dict_approved":     e.get("dict_approved"),
            "domain_id":         e.get("domain_id"),
            "assigned_domain":   e.get("assigned_domain"),
            "domain_confidence": e.get("domain_confidence") or 0,
            "entity_id":         e.get("entity_id"),
            "assigned_entity":   e.get("assigned_entity"),
            "entity_confidence": e.get("entity_confidence") or 0,
        })
    return rows


def _filter_discovery_rows(
    rows: list[dict], *, asset_type, schema, domain, entity, pii,
    dictionary_status, classification, profile_status,
) -> list[dict]:
    """Python-side equivalent of the SQL filters applied to the legacy
    profiling-anchored query, so discovery-anchored search supports the
    exact same server-side filters."""
    if asset_type == "domain":
        rows = [d for d in rows if d.get("assigned_domain") or d.get("dict_domain")]
    elif asset_type == "entity":
        rows = [d for d in rows if d.get("assigned_entity")]
    elif asset_type == "dictionary":
        rows = [d for d in rows if d.get("business_name") or d.get("description")]

    if schema:
        rows = [d for d in rows if d.get("schema_name") == schema]
    if domain:
        rows = [d for d in rows if d.get("assigned_domain") == domain or d.get("dict_domain") == domain]
    if entity:
        rows = [d for d in rows if d.get("assigned_entity") == entity]
    if pii is True:
        rows = [d for d in rows if (d.get("pii_column_count") or 0) > 0]
    if dictionary_status == "approved":
        rows = [d for d in rows if d.get("dict_approved")]
    elif dictionary_status == "generated":
        rows = [d for d in rows if d.get("business_name") and not d.get("dict_approved")]
    elif dictionary_status == "none":
        rows = [d for d in rows if not d.get("business_name")]
    if classification:
        rows = [d for d in rows if d.get("table_class") == classification]
    if profile_status:
        rows = [d for d in rows if d.get("profiling_status") == profile_status]

    return rows


def _legacy_profiling_anchored_rows(
    cursor, expanded_tokens: list[str], *, source_id: Optional[int],
    asset_type, schema, domain, entity, pii, dictionary_status, classification, profile_status,
) -> list:
    """The original profiling_table_profiles-anchored table query, unchanged.
    Used for cross-source search (source_id=None) and as the fallback when
    a specific source has no discoverable schema_snapshots row at all."""
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

    if schema:
        sql += " AND ptp.schema_name = ?"
        params.append(schema)
    if domain:
        sql += " AND (da.domain = ? OR ddt.domain = ?)"
        params.extend([domain, domain])
    if entity:
        sql += " AND ea.entity = ?"
        params.append(entity)
    if pii is True:
        sql += " AND ptp.pii_column_count > 0"
    if dictionary_status == "approved":
        sql += " AND ddt.is_approved = 1"
    elif dictionary_status == "generated":
        sql += " AND ddt.business_name IS NOT NULL AND (ddt.is_approved = 0 OR ddt.is_approved IS NULL)"
    elif dictionary_status == "none":
        sql += " AND ddt.business_name IS NULL"
    if classification:
        sql += " AND ptp.table_class = ?"
        params.append(classification)
    if profile_status:
        sql += " AND ptp.profiling_status = ?"
        params.append(profile_status)
    # semantic_type is a column-level concept; ignored for table results

    sql += f" LIMIT {_MAX_CANDIDATES}"
    return cursor.execute(sql, params).fetchall()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_metadata(
    q: str,
    limit: int = 20,
    offset: int = 0,
    source_id: Optional[int] = None,
    asset_type: Optional[str] = None,
    schema: Optional[str] = None,
    domain: Optional[str] = None,
    entity: Optional[str] = None,
    semantic_type: Optional[str] = None,
    pii: Optional[bool] = None,
    dictionary_status: Optional[str] = None,
    classification: Optional[str] = None,
    profile_status: Optional[str] = None,
) -> dict:
    """
    Keyword search across all stored metadata with server-side filtering.

    Parameters
    ----------
    q                 : search query string
    limit             : max results per page (1–100)
    offset            : pagination offset
    source_id         : restrict to a single data source
    asset_type        : 'table', 'column', 'dictionary', 'domain', 'entity'; None = all
    schema            : exact schema_name filter
    domain            : exact domain filter
    entity            : exact entity filter
    semantic_type     : exact semantic_type filter (columns)
    pii               : True = only PII assets
    dictionary_status : 'approved', 'generated', or 'none'
    classification    : exact table_class filter
    profile_status    : exact profiling_status filter

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
            discovered = _discovered_tables(conn, source_id) if source_id is not None else []
            if discovered:
                # Discovery-anchored: every table from the latest schema
                # snapshot is a candidate, whether or not profiling or
                # dictionary generation has run yet (Sprint 1.1).
                table_rows = _discovery_anchored_table_rows(conn, source_id, discovered)
                table_rows = _filter_discovery_rows(
                    table_rows, asset_type=asset_type, schema=schema, domain=domain,
                    entity=entity, pii=pii, dictionary_status=dictionary_status,
                    classification=classification, profile_status=profile_status,
                )
                table_rows = table_rows[:_MAX_CANDIDATES]
            else:
                # Cross-source search (source_id=None), or a specific source
                # with no schema_snapshots row at all — unchanged, still
                # anchored on profiling_table_profiles.
                table_rows = [dict(r) for r in _legacy_profiling_anchored_rows(
                    cursor, expanded_tokens, source_id=source_id,
                    asset_type=asset_type, schema=schema, domain=domain, entity=entity,
                    pii=pii, dictionary_status=dictionary_status,
                    classification=classification, profile_status=profile_status,
                )]

            for row in table_rows:
                d = dict(row)
                base_score, token_reasons = _score_table_detailed(d, expanded_tokens)
                if base_score <= 0:
                    continue

                reasons = list(token_reasons)
                score   = base_score

                # Exact phrase bonus — full query string found verbatim in a key field
                if _check_phrase(d, _TABLE_PHRASE_FIELDS, q):
                    score  += _PHRASE_BONUS
                    reasons = ["Exact phrase match"] + reasons

                # Multi-field bonus — breadth of token-matched evidence
                score += _multi_field_bonus(len(token_reasons))

                # Non-authoritative naming penalty (Sprint 1.2, Fix 2) —
                # penalizes, never excludes; base_score already passed the
                # continue-gate above, so a heavily-penalized real match is
                # still returned, just ranked lower.
                penalty = _naming_penalty(d.get("table_name") or "")
                if penalty:
                    score -= penalty
                    reasons.append(f"Naming penalty (-{penalty})")

                matched_f, matched_t = _best_match(d, expanded_tokens, _TABLE_FIELD_PRIORITY)
                result_domain = d.get("assigned_domain") or d.get("dict_domain") or ""
                result_entity = d.get("assigned_entity") or ""
                result_pii    = bool(d.get("pii_column_count") and d["pii_column_count"] > 0)
                confidence    = round(max(
                    float(d.get("domain_confidence") or 0),
                    float(d.get("entity_confidence") or 0),
                ), 2)
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
                    "match_reasons":     reasons,
                    "short_description": (d.get("description") or "")[:200],
                    "domain":            result_domain,
                    "entity":            result_entity,
                    "dictionary_status": (
                        "approved"  if d.get("dict_approved")
                        else "generated" if d.get("business_name")
                        else "none"
                    ),
                    "pii_indicator":     result_pii,
                    "semantic_type":     d.get("table_class") or "",
                    "table_type":        d.get("table_type") or "TABLE",
                    "confidence":        confidence,
                    "profiled_at":       d.get("profiled_at") or "",
                    "profiling_status":  d.get("profiling_status") or "",
                    "nav_target":        _table_nav_target(d, asset_type),
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

            # ── Server-side field filters ──────────────────────────────────
            if schema:
                sql += " AND ptp.schema_name = ?"
                params.append(schema)
            if domain:
                sql += " AND da.domain = ?"
                params.append(domain)
            if entity:
                sql += " AND ea.entity = ?"
                params.append(entity)
            if semantic_type:
                sql += " AND pcp.semantic_type = ?"
                params.append(semantic_type)
            if pii is True:
                sql += " AND (pcp.pii_confirmed = 1 OR ddc.pii_risk = 1)"
            if dictionary_status == "approved":
                sql += " AND ddc.is_approved = 1"
            elif dictionary_status == "generated":
                sql += " AND ddc.business_label IS NOT NULL AND (ddc.is_approved = 0 OR ddc.is_approved IS NULL)"
            elif dictionary_status == "none":
                sql += " AND ddc.business_label IS NULL"
            if classification:
                sql += " AND ptp.table_class = ?"
                params.append(classification)
            if profile_status:
                sql += " AND pcp.profiling_status = ?"
                params.append(profile_status)

            sql += f" LIMIT {_MAX_CANDIDATES}"
            rows = cursor.execute(sql, params).fetchall()

            for row in rows:
                d = dict(row)
                base_score, token_reasons = _score_column_detailed(d, expanded_tokens)
                if base_score <= 0:
                    continue

                reasons = list(token_reasons)
                score   = base_score

                # Exact phrase bonus
                if _check_phrase(d, _COLUMN_PHRASE_FIELDS, q):
                    score  += _PHRASE_BONUS
                    reasons = ["Exact phrase match"] + reasons

                # Multi-field bonus
                score += _multi_field_bonus(len(token_reasons))

                matched_f, matched_t = _best_match(d, expanded_tokens, _COLUMN_FIELD_PRIORITY)
                result_domain = d.get("assigned_domain") or ""
                result_entity = d.get("assigned_entity") or ""
                result_pii    = bool(d.get("pii_confirmed") or d.get("pii_risk"))
                confidence    = round(max(
                    float(d.get("domain_confidence") or 0),
                    float(d.get("entity_confidence") or 0),
                ), 2)
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
                    "match_reasons":     reasons,
                    "short_description": (d.get("meaning") or "")[:200],
                    "domain":            result_domain,
                    "entity":            result_entity,
                    "dictionary_status": (
                        "approved"  if d.get("dict_approved")
                        else "generated" if d.get("business_label")
                        else "none"
                    ),
                    "pii_indicator":     result_pii,
                    "semantic_type":     d.get("semantic_type") or "",
                    "table_type":        None,
                    "confidence":        confidence,
                    "profiled_at":       d.get("profiled_at") or "",
                    "profiling_status":  d.get("profiling_status") or "",
                    "nav_target":        _column_nav_target(d, asset_type),
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


# ---------------------------------------------------------------------------
# Filter metadata — returns available values for every filterable dimension
# ---------------------------------------------------------------------------

def get_search_filters() -> dict:
    """Return distinct filter values that actually exist in the metadata catalog.

    Only values present in the database are returned — the frontend uses this
    to hide filter controls that would always yield zero results.  No AI, no
    embeddings; all values come directly from stored profiling and dictionary
    metadata.
    """
    conn = get_connection()
    try:
        sources = [
            {"id": row["id"], "name": row["display_name"]}
            for row in conn.execute(
                "SELECT id, display_name FROM data_source_connections"
                " WHERE is_active = 1 ORDER BY display_name"
            ).fetchall()
        ]

        schemas = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT schema_name FROM profiling_table_profiles"
                " WHERE schema_name IS NOT NULL AND schema_name != ''"
                " ORDER BY schema_name"
            ).fetchall()
        ]

        domain_rows = conn.execute(
            "SELECT DISTINCT domain FROM domain_assignments"
            " WHERE domain IS NOT NULL AND domain != ''"
            " UNION"
            " SELECT DISTINCT domain FROM data_dictionary_tables"
            " WHERE domain IS NOT NULL AND domain != ''"
            " ORDER BY domain"
        ).fetchall()
        domains = [r[0] for r in domain_rows]

        entity_rows = conn.execute(
            "SELECT DISTINCT entity FROM entity_assignments"
            " WHERE entity IS NOT NULL AND entity != ''"
            " ORDER BY entity"
        ).fetchall()
        entities = [r[0] for r in entity_rows]

        sem_rows = conn.execute(
            "SELECT DISTINCT semantic_type FROM profiling_column_profiles"
            " WHERE semantic_type IS NOT NULL AND semantic_type != ''"
            " UNION"
            " SELECT DISTINCT semantic_type FROM data_dictionary_columns"
            " WHERE semantic_type IS NOT NULL AND semantic_type != ''"
            " ORDER BY semantic_type"
        ).fetchall()
        semantic_types = [r[0] for r in sem_rows]

        class_rows = conn.execute(
            "SELECT DISTINCT table_class FROM profiling_table_profiles"
            " WHERE table_class IS NOT NULL AND table_class != ''"
            " ORDER BY table_class"
        ).fetchall()
        classifications = [r[0] for r in class_rows]

        status_rows = conn.execute(
            "SELECT DISTINCT profiling_status FROM profiling_table_profiles"
            " WHERE profiling_status IS NOT NULL AND profiling_status != ''"
            " ORDER BY profiling_status"
        ).fetchall()
        profile_statuses = [r[0] for r in status_rows]

        pii_row = conn.execute(
            "SELECT 1 FROM profiling_table_profiles WHERE pii_column_count > 0 LIMIT 1"
        ).fetchone()
        pii_available = pii_row is not None

        approved_row = conn.execute(
            "SELECT 1 FROM data_dictionary_tables WHERE is_approved = 1 LIMIT 1"
        ).fetchone()
        generated_row = conn.execute(
            "SELECT 1 FROM data_dictionary_tables"
            " WHERE business_name IS NOT NULL AND (is_approved = 0 OR is_approved IS NULL)"
            " LIMIT 1"
        ).fetchone()
        dict_statuses: list[str] = []
        if approved_row:
            dict_statuses.append("approved")
        if generated_row:
            dict_statuses.append("generated")
        if approved_row or generated_row:
            dict_statuses.append("none")

    finally:
        conn.close()

    return {
        "sources":             sources,
        "schemas":             schemas,
        "domains":             domains,
        "entities":            entities,
        "semantic_types":      semantic_types,
        "classifications":     classifications,
        "profile_statuses":    profile_statuses,
        "dictionary_statuses": dict_statuses,
        "pii_available":       pii_available,
    }


# ---------------------------------------------------------------------------
# Autocomplete suggestions — drawn exclusively from real stored metadata
# ---------------------------------------------------------------------------

def get_search_suggestions(q: str, limit: int = 8) -> list[dict]:
    """Return up to *limit* autocomplete suggestions from real metadata.

    Searches table names, business names, column names, business labels,
    domain assignments, and entity assignments.  No suggestion is hardcoded —
    all values come from stored profiling and dictionary metadata.

    Results are sorted so prefix matches (starts-with) rank before
    contains-only matches, then alphabetically within each group.

    Parameters
    ----------
    q     : partial query string (min length checked by caller)
    limit : maximum suggestions to return (capped at 20 by the route layer)

    Returns
    -------
    list of {"text": str, "type": str} dicts
    """
    if not q or not q.strip():
        return []

    q_clean = q.strip()
    q_lower = q_clean.lower()
    like    = f"%{q_lower}%"

    conn = get_connection()
    candidates: list[dict] = []
    try:
        for sql, stype in (
            (
                "SELECT DISTINCT table_name AS t FROM profiling_table_profiles"
                " WHERE LOWER(table_name) LIKE ? AND table_name IS NOT NULL LIMIT 50",
                "table",
            ),
            (
                "SELECT DISTINCT business_name AS t FROM data_dictionary_tables"
                " WHERE LOWER(business_name) LIKE ? AND business_name IS NOT NULL LIMIT 50",
                "business_name",
            ),
            (
                "SELECT DISTINCT column_name AS t FROM profiling_column_profiles"
                " WHERE LOWER(column_name) LIKE ? AND column_name IS NOT NULL LIMIT 50",
                "column",
            ),
            (
                "SELECT DISTINCT business_label AS t FROM data_dictionary_columns"
                " WHERE LOWER(business_label) LIKE ? AND business_label IS NOT NULL LIMIT 50",
                "business_name",
            ),
            (
                "SELECT DISTINCT domain AS t FROM domain_assignments"
                " WHERE LOWER(domain) LIKE ? AND domain IS NOT NULL LIMIT 20",
                "domain",
            ),
            (
                "SELECT DISTINCT entity AS t FROM entity_assignments"
                " WHERE LOWER(entity) LIKE ? AND entity IS NOT NULL LIMIT 20",
                "entity",
            ),
        ):
            for row in conn.execute(sql, (like,)).fetchall():
                if row[0]:
                    candidates.append({"text": row[0], "type": stype})
    finally:
        conn.close()

    # Deduplicate by lower-cased text, preserving first occurrence per source order
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        key = c["text"].lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Prefix matches first, then alphabetical within each group
    unique.sort(
        key=lambda c: (0 if c["text"].lower().startswith(q_lower) else 1, c["text"].lower())
    )

    return unique[:limit]
